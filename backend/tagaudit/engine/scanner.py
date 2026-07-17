"""
engine/scanner.py - ZimaTAG Background Scanner
Moteur de scan autonome avec persistance CSV.

Corrections appliquées :
  [14] Gestion robuste des PermissionError / OSError lors du parcours du
       système de fichiers : un dossier inaccessible n'arrête plus le scan,
       il est seulement loggé et ignoré (idem fichier sans droit de stat).
  [27] Throttling des écritures de l'état de scan : au lieu d'écrire
       scan_state.json à CHAQUE fichier traité (50 000 écritures sur une
       grosse collection), on regroupe les mises à jour à intervalle régulier
       (par défaut 0.5 s). La progression visible côté UI reste fluide
       (rafraîchissement Streamlit déjà à ~1 s) et les compteurs finaux
       sont toujours flushés en fin de scan, lors d'un stop, ou en cas
       d'erreur.
"""
import os
import time
import json
import threading
from pathlib import Path
from typing import List, Generator, Optional, Set
from collections import deque
from datetime import datetime
from core import logger, config, state_manager, db
from engine.smart_extractor import SmartExtractor


class BackgroundScanner:
    """Moteur de scan autonome en arrière-plan"""
    
    CSV_COLUMNS = [
        'filepath', 'filename', 'extension', 'directory', 'parent_folder',
        'size_mb', 'modified_date', 'file_md5', 'title', 'artist', 'album', 'albumartist',
        'composer', 'genre', 'year', 'track', 'tracktotal', 'disc', 'disctotal',
        'encoder', 'duration', 'duration_seconds', 'bitrate', 'samplerate',
        'channels', 'bitdepth', 'codec', 'id3_version', 'has_cover',
        'cover_size', 'cover_format', 'cover_width', 'cover_height',
        'cover_md5', 'cover_valid', 'cover_error', 'cover_count', 'error'
    ]
    
    # [27] Intervalle minimum entre deux écritures de l'état (en secondes).
    # Réduit drastiquement le coût I/O sur grosse collection sans dégrader
    # la perception de progression côté UI (qui rafraîchit elle-même à ~1 s).
    STATE_UPDATE_INTERVAL: float = 0.5
    
    def __init__(self, scan_paths: List[Path], formats: List[str] = None, file_limit: int = None):
        self.scan_paths = scan_paths
        self.formats = set(formats) if formats else config.AUDIO_EXTENSIONS
        self.file_limit = file_limit
        self.extractor = SmartExtractor()
        self._stop_event = threading.Event()
        self._buffer: deque = deque(maxlen=config.BATCH_SIZE)
        self._last_flush = time.time()
        self._speed_samples: deque = deque(maxlen=20)
        self._files_list: List[Path] = []
        # [27] Horodatage de la dernière mise à jour de state envoyée au manager
        self._last_state_update: float = 0.0
        # [LOT v20-2] Connexion SQLite de la double écriture, une seule pour
        # tout le scan (voir _init_csv/_flush_buffer_sqlite/_run_scan)
        self._db_conn = None
        
    def prescan(self) -> int:
        """Pré-scan rapide pour compter les fichiers"""
        logger.info(f"Démarrage pré-scan... Formats: {self.formats}, Limite: {self.file_limit}")
        start = time.time()
        self._files_list = list(self._iter_audio_files())
        
        # Applique la limite si définie
        if self.file_limit and len(self._files_list) > self.file_limit:
            self._files_list = self._files_list[:self.file_limit]
            logger.info(f"Limite appliquée: {self.file_limit} fichiers")
        
        total = len(self._files_list)
        elapsed = time.time() - start
        
        # Comptage par format
        self._count_mp3 = sum(1 for f in self._files_list if f.suffix.lower() == '.mp3')
        self._count_flac = sum(1 for f in self._files_list if f.suffix.lower() == '.flac')
        self._count_m4a = sum(1 for f in self._files_list if f.suffix.lower() == '.m4a')
        
        # Sauvegarde résultat pré-scan
        prescan_data = {
            'total_files': total,
            'total_mp3': self._count_mp3,
            'total_flac': self._count_flac,
            'total_m4a': self._count_m4a,
            'scan_paths': [str(p) for p in self.scan_paths],
            'formats': list(self.formats),
            'file_limit': self.file_limit,
            'prescan_time': round(elapsed, 2),
            'timestamp': datetime.now().isoformat()
        }
        with open(config.prescan_path, 'w', encoding='utf-8') as f:
            json.dump(prescan_data, f, indent=2)
        
        logger.info(f"Pré-scan terminé: {total} fichiers (MP3:{self._count_mp3}, FLAC:{self._count_flac}, M4A:{self._count_m4a}) en {elapsed:.1f}s")
        return total
    
    def start(self) -> bool:
        """Démarre le scan en arrière-plan"""
        logger.debug("[SCANNER] Tentative acquire_lock")
        if not state_manager.acquire_lock():
            logger.error("Scan déjà en cours")
            return False
        
        logger.debug("[SCANNER] Lock acquis")
        self._stop_event.clear()
        
        try:
            self._run_scan()
        except Exception as e:
            logger.error(f"Erreur scan: {e}")
            state_manager.update(status='error', last_error=str(e))
        finally:
            try:
                self._flush_buffer()
            except Exception as e:
                logger.error(f"[SCANNER] Échec du flush de secours (finally): {e}")
            logger.debug("[SCANNER] Libération lock")
            state_manager.release_lock()
        
        return True
    
    def stop(self):
        """Arrête le scan"""
        self._stop_event.set()
        logger.info("Arrêt demandé")
    
    def _run_scan(self):
        """Exécute le scan principal"""
        total = len(self._files_list)
        if total == 0:
            total = self.prescan()
        
        if total == 0:
            logger.warning("Aucun fichier audio trouvé")
            state_manager.update(status='completed', total_files=0)
            return
        
        # Compteurs par format (si pas encore calculés)
        if not hasattr(self, '_count_mp3'):
            self._count_mp3 = sum(1 for f in self._files_list if f.suffix.lower() == '.mp3')
            self._count_flac = sum(1 for f in self._files_list if f.suffix.lower() == '.flac')
            self._count_m4a = sum(1 for f in self._files_list if f.suffix.lower() == '.m4a')
        
        state_manager.update(
            status='running',
            total_files=total,
            processed_files=0,
            start_time=time.time(),
            pid=os.getpid(),
            total_mp3=self._count_mp3,
            total_flac=self._count_flac,
            total_m4a=self._count_m4a,
            done_mp3=0,
            done_flac=0,
            done_m4a=0
        )
        
        # Prépare fichier CSV
        self._init_csv()
        
        start = time.time()
        processed = 0
        done_mp3 = 0
        done_flac = 0
        done_m4a = 0
        
        # [27] Initialise le timer de throttling avec un offset négatif pour
        # garantir une première mise à jour rapide après quelques fichiers.
        self._last_state_update = start - self.STATE_UPDATE_INTERVAL
        
        for filepath in self._files_list:
            if self._stop_event.is_set():
                break
            
            # Extraction
            data = self.extractor.extract(filepath)
            self._buffer.append(data)
            processed += 1
            
            # Compteur par format
            ext = filepath.suffix.lower()
            if ext == '.mp3':
                done_mp3 += 1
            elif ext == '.flac':
                done_flac += 1
            elif ext == '.m4a':
                done_m4a += 1
            
            # [27] Mise à jour état throttlée : on n'écrit le scan_state.json
            # qu'au plus toutes les STATE_UPDATE_INTERVAL secondes, sauf en
            # toute fin de scan (dernier fichier) où on force la mise à jour
            # pour que les compteurs finaux soient corrects côté UI.
            now = time.time()
            is_last_file = (processed == total)
            if (now - self._last_state_update) >= self.STATE_UPDATE_INTERVAL or is_last_file:
                elapsed = now - start
                speed = processed / elapsed if elapsed > 0 else 0
                self._speed_samples.append(speed)
                avg_speed = sum(self._speed_samples) / len(self._speed_samples)
                remaining = total - processed
                eta = remaining / avg_speed if avg_speed > 0 else 0
                
                state_manager.update(
                    processed_files=processed,
                    current_file=filepath.name,
                    current_dir=str(filepath.parent),
                    elapsed_time=elapsed,
                    speed=round(avg_speed, 1),
                    eta_seconds=round(eta),
                    done_mp3=done_mp3,
                    done_flac=done_flac,
                    done_m4a=done_m4a
                )
                self._last_state_update = now
            
            # Flush si nécessaire
            if len(self._buffer) >= config.BATCH_SIZE or \
               time.time() - self._last_flush >= config.FLUSH_INTERVAL:
                self._flush_buffer()
        
        # Final
        self._flush_buffer()

        # [LOT v20-2] Fermeture propre SQLite : checkpoint WAL explicite avant
        # close() (garantit que les donnees sont dans master_scan.db, pas
        # seulement -wal -- necessaire pour la Preuve A qui regenerera un CSV
        # depuis ce fichier). S'execute que le scan finisse normalement ou
        # soit arrete (_stop_event), les deux chemins passent par ici.
        if self._db_conn is not None:
            try:
                self._db_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                self._db_conn.close()
            except Exception as e:
                logger.error(f"[SQLITE] Erreur fermeture/checkpoint: {e}")
            finally:
                self._db_conn = None

        # [27] Force une dernière mise à jour de l'état pour garantir que
        # les compteurs finaux sont visibles côté UI, même si on a été
        # interrompu juste après un throttle.
        elapsed = time.time() - start
        final_status = 'completed' if not self._stop_event.is_set() else 'paused'
        state_manager.update(
            status=final_status,
            processed_files=processed,
            elapsed_time=elapsed,
            done_mp3=done_mp3,
            done_flac=done_flac,
            done_m4a=done_m4a,
        )
        logger.info(f"Scan terminé: {processed}/{total} fichiers")
    
    # ------------------------------------------------------------------
    # Itération sur les fichiers audio (robuste aux erreurs FS)
    # ------------------------------------------------------------------
    def _iter_audio_files(self) -> Generator[Path, None, None]:
        """Itère sur tous les fichiers audio selon les formats sélectionnés.
        
        [14] Robuste aux erreurs de système de fichiers :
          - PermissionError sur un dossier : loggé et ignoré, on continue.
          - OSError (fichier disparu, lien cassé, etc.) : idem.
          - Une erreur sur UN chemin n'arrête pas le scan des AUTRES chemins.
        
        Note : pour conserver le comportement existant, on utilise toujours
        rglob("*") filtre par suffix.lower() : insensible a la casse
        (les extensions majuscules comme .MP3 sont desormais captees).
        """
        for scan_path in self.scan_paths:
            try:
                if not scan_path.exists():
                    logger.warning(f"Chemin inexistant: {scan_path}")
                    continue
            except (PermissionError, OSError) as e:
                # exists() peut échouer si le parent est inaccessible
                logger.warning(f"Chemin inaccessible {scan_path}: {e}")
                continue
            
            # Un SEUL parcours rglob("*"), filtre sur suffix.lower() :
            # insensible a la casse (rglob unique) -> capte .mp3 comme .MP3.
            # _safe_rglob conserve la tolerance PermissionError/OSError.
            exts_lower = {e.lower() for e in self.formats}
            for entry in self._safe_rglob(scan_path, "*"):
                if entry.suffix.lower() in exts_lower:
                    yield entry
    
    def _safe_rglob(self, root: Path, pattern: str) -> Generator[Path, None, None]:
        """Variante tolérante de Path.rglob qui ne plante pas sur un
        sous-dossier inaccessible.
        
        [14] PermissionError / OSError dans un sous-arbre = on logge une
        seule fois et on saute, sans interrompre le reste du scan.
        """
        try:
            iterator = root.rglob(pattern)
        except (PermissionError, OSError) as e:
            logger.warning(f"[SCANNER] Impossible d'itérer {root} ({pattern}): {e}")
            return
        
        # Itération défensive : Path.rglob est un générateur, et l'erreur
        # peut survenir à chaque appel `next()` quand il descend dans un
        # sous-dossier interdit. On consomme donc en boucle avec try/except.
        while True:
            try:
                entry = next(iterator)
            except StopIteration:
                return
            except (PermissionError, OSError) as e:
                # Erreur sur un sous-arbre : on logge et on continue.
                # rglob() interne peut continuer sur les autres branches après
                # ce skip, donc on relance le `next()` plutôt que de return.
                logger.warning(f"[SCANNER] Sous-dossier ignoré dans {root}: {e}")
                continue
            yield entry
    
    def _init_csv(self):
        """[LOT v20-6] Initialise la table SQLite tracks (ecrase l'ancienne).
        Le CSV n'est plus ecrit -- master_scan.db est desormais l'unique
        source. Un echec ici interrompt le scan (raise) : plus de CSV en
        filet, un scan qui "reussit" sans donnees ecrites serait pire
        qu'un echec visible."""
        db.init_schema()
        self._db_conn = db.connect()
        self._db_conn.execute("DELETE FROM tracks")
        self._db_conn.commit()
        logger.info(f"SQLite initialisé: {db.DB_PATH}")

    def _flush_buffer(self):
        """[LOT v20-6] Ecrit le buffer dans SQLite (CSV retire, coupure seche)."""
        if not self._buffer:
            return

        self._flush_buffer_sqlite()
        self._buffer.clear()
        self._last_flush = time.time()

    def _flush_buffer_sqlite(self):
        """[LOT v20-6] Ecrit le buffer dans SQLite -- unique source depuis la
        coupure du CSV. Un echec ici (y compris IntegrityError sur un
        doublon filepath) interrompt le scan : plus de CSV en filet.
        """
        # Normalisation ligne par ligne : le CSV convertissait
        # silencieusement None -> '' et stringifiait tout via str() a
        # l'ecriture. sqlite3 ne fait ni l'un ni l'autre -- decision figee
        # reproduite ici explicitement.
        rows = [
            tuple('' if row.get(col) is None else str(row.get(col)) for col in self.CSV_COLUMNS)
            for row in self._buffer
        ]
        columns = ','.join(self.CSV_COLUMNS)
        placeholders = ','.join('?' for _ in self.CSV_COLUMNS)
        sql = f"INSERT INTO tracks ({columns}) VALUES ({placeholders})"

        with self._db_conn:  # commit auto si OK, rollback auto si exception
            self._db_conn.executemany(sql, rows)

    def _handle_signal(self, signum, frame):
        """Gère les signaux système (appelé depuis l'extérieur)"""
        logger.info(f"Signal {signum} reçu")
        self.stop()
