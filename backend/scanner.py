"""ZimaCompare v3.4 — Scanner avec lock source.

NEW v3.4 : snapshot du mtime du dossier source en début ET en fin de scan.
Si ça a changé, on lève un warning visible dans l'UI (le scan reste valide
mais l'utilisateur sait qu'il a peut-être loupé des écritures concurrentes).
"""
import csv
import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple

from comparators import (
    compare_files, hash_cache_load, hash_cache_save, hash_cache_stats,
    sha1_cache_load, sha1_cache_save, sha1_cache_stats, compare_cloud,
)
from config import (
    SCAN_CSV, IGNORED_FILES_JSON, AppState,
    load_ignore_patterns, setup_logging, update_state, add_paths_to_history,
    compile_ignore_spec, ignore_match, compile_individual_specs, ignore_which,
)

logger = setup_logging()

CSV_COLUMNS = [
    "relative_path", "source_size", "source_mtime", "target_size", "target_mtime",
    "is_dir", "status", "reason", "source_hash", "target_hash",
]

_stop_event = threading.Event()


def stop_scan():
    _stop_event.set()
    logger.warning("[SCAN] Drapeau d'arrêt levé — interruption demandée")


def _should_ignore(name: str, patterns: List[str]) -> bool:
    """v3.12 — Conservé pour compatibilité ascendante (fallback naïf si pathspec
    indisponible). Ne pas utiliser directement : préférer `ignore_match(spec, …)`.
    """
    return any(p == name for p in patterns)


def _dir_signature(path: Path) -> Tuple[float, int]:
    """Snapshot léger d'un dossier : mtime racine + nombre d'entrées immédiates.
    Sert à détecter les modifications de la source pendant le scan."""
    try:
        st = path.stat()
        # On compte aussi les entrées top-level pour attraper l'ajout/suppression
        # d'un sous-dossier qui ne déclencherait pas forcément un mtime parent.
        with os.scandir(path) as it:
            count = sum(1 for _ in it)
        return (st.st_mtime, count)
    except Exception:
        return (0.0, 0)


# v3.13 — plafond du nombre de chemins ignorés enregistrés en détail.
# Au-delà, on garde uniquement le compteur (évite un JSON de plusieurs Mo).
_IGNORED_LIST_CAP = 10_000


def _scandir_recursive(directory: Path, spec, label: str,
                       individual_specs=None, ignored_sink: List[dict] = None, name_filter: str = ""
                       ) -> Tuple[Dict[str, Tuple[int, float, bool]], int]:
    """Parcourt récursivement `directory` en honorant le `spec` PathSpec
    (gitignore-style). Retourne (entries, ignored_count).

    v3.12 : optimisation — si un dossier est ignoré, on ne descend pas dedans.
    v3.13 : si `ignored_sink` est fourni, chaque chemin ignoré y est ajouté
            (jusqu'à _IGNORED_LIST_CAP entrées) avec son pattern déclencheur.
    """
    out: Dict[str, Tuple[int, float, bool]] = {}
    count = 0
    ignored = 0
    base_str = str(directory)
    base_len = len(base_str) + 1

    def _record_ignored(rel: str, is_dir: bool, size: int):
        """Ajoute une entrée à la liste détaillée, sous le plafond."""
        if ignored_sink is None or len(ignored_sink) >= _IGNORED_LIST_CAP:
            return
        pattern = ""
        if individual_specs:
            pattern = ignore_which(individual_specs, rel, is_dir)
        ignored_sink.append({
            "relative_path": rel,
            "is_dir":        is_dir,
            "size":          size,
            "side":          label,        # "source" ou "cible"
            "pattern":       pattern,
        })

    def _walk(path: str):
        nonlocal count, ignored
        if _stop_event.is_set():
            return
        try:
            with os.scandir(path) as it:
                for entry in it:
                    if _stop_event.is_set():
                        return
                    if path == base_str and name_filter and name_filter not in entry.name.lower():
                        continue
                    full = entry.path
                    rel  = full[base_len:] if len(full) > base_len else entry.name
                    try:
                        is_dir = entry.is_dir(follow_symlinks=False)
                    except OSError:
                        continue
                    # v3.12 : test gitignore sur le chemin relatif complet
                    if ignore_match(spec, rel, is_dir):
                        ignored += 1
                        # taille : 0 pour un dossier, sinon stat du fichier
                        sz = 0
                        if not is_dir:
                            try:
                                sz = entry.stat(follow_symlinks=False).st_size
                            except OSError:
                                sz = 0
                        _record_ignored(rel, is_dir, sz)
                        # Si c'est un dossier ignoré, on ne descend pas dedans
                        # (cohérent avec le comportement git).
                        continue
                    try:
                        st = entry.stat(follow_symlinks=False)
                        if is_dir:
                            out[rel] = (0, st.st_mtime, True)
                            _walk(full)
                        else:
                            out[rel] = (st.st_size, st.st_mtime, False)
                        count += 1
                        if count % 1000 == 0:
                            update_state(
                                current_file=f"Collecte {label}… {count} entrées",
                                ignored_count=ignored,
                            )
                    except (PermissionError, FileNotFoundError, OSError) as e:
                        logger.debug(f"[SCAN] skip {full}: {e}")
        except (PermissionError, FileNotFoundError, OSError) as e:
            logger.warning(f"[SCAN] {path}: {e}")

    _walk(base_str)
    return out, ignored


def _write_ignored_files(entries: List[dict], capped: bool, total: int):
    """Écrit la liste des fichiers ignorés dans IGNORED_FILES_JSON (atomique)."""
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "total":        total,             # compteur réel (peut dépasser len(entries))
        "listed":       len(entries),      # nombre effectivement enregistré
        "capped":       capped,            # True si on a atteint le plafond
        "cap":          _IGNORED_LIST_CAP,
        "items":        entries,
    }
    try:
        IGNORED_FILES_JSON.parent.mkdir(parents=True, exist_ok=True)
        tmp = IGNORED_FILES_JSON.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        tmp.replace(IGNORED_FILES_JSON)
    except Exception as e:
        logger.warning(f"[SCAN] Impossible d'écrire {IGNORED_FILES_JSON.name}: {e}")


def _run_scan(source: str, target: str, method: str, chunk_mb: int, name_filter: str = ""):
    src = Path(source)
    tgt = Path(target)
    _nf = (name_filter or "").strip().lower()
    # v3.12 — compile la spec gitignore une fois pour tout le scan
    patterns = load_ignore_patterns()
    spec = compile_ignore_spec(patterns)
    # v3.13 — specs individuels pour identifier le pattern déclencheur
    individual_specs = compile_individual_specs(patterns)
    ignored_sink: List[dict] = []

    try:
        logger.info(f"[SCAN] Démarrage — source={source} target={target} method={method}")
        logger.info(f"[SCAN] {len(patterns)} pattern(s) .zimaignore chargé(s)"
                    f"{' (matcher gitignore)' if spec else ' (fallback nom exact)'}")
        add_paths_to_history(source, target)

        # NEW v3.4 : snapshot du signature source au début
        sig_start = _dir_signature(src)
        tsig_start = _dir_signature(tgt)
        logger.info(f"[SCAN] Signature source initiale : mtime={sig_start[0]:.0f}, entries={sig_start[1]}")

        _is_cloud = (method == "cloud")
        _cache_load = sha1_cache_load if _is_cloud else hash_cache_load
        _cache_save = sha1_cache_save if _is_cloud else hash_cache_save
        _cache_stats = sha1_cache_stats if _is_cloud else hash_cache_stats
        cache_kind = "SHA1" if _is_cloud else "xxhash"
        _cache_load()
        cache_start = _cache_stats()
        logger.info(f"[SCAN] Cache {cache_kind} : {cache_start['entries']} entrées chargées")

        update_state(app_state=AppState.SCANNING, current_file="Collecte source…",
                     progress=0, processed=0, total=0, scan_done=False,
                     new_count=0, different_count=0, deleted_count=0, identical_count=0,
                     bytes_to_copy=0, source_changed=False, source_warning="",
                     fps=0, eta_seconds=0,  # pas de débit pertinent en phase de collecte
                     ignored_count=0)  # v3.12

        t0 = time.monotonic()
        src_entries, src_ignored = _scandir_recursive(
            src, spec, "source", individual_specs, ignored_sink, _nf)
        if _stop_event.is_set():
            update_state(app_state=AppState.IDLE, current_file="Annulé", progress=0); return
        t1 = time.monotonic()
        logger.info(f"[SCAN] Source collectée : {len(src_entries)} entrées en {t1-t0:.1f}s"
                    f" — {src_ignored} ignorée(s) par .zimaignore")

        update_state(current_file=f"Collecte cible… (source : {len(src_entries)} entrées)",
                     fps=0, eta_seconds=0,  # débit non pertinent ici
                     ignored_count=src_ignored)
        tgt_entries, tgt_ignored = _scandir_recursive(
            tgt, spec, "cible", individual_specs, ignored_sink, _nf)
        if _stop_event.is_set():
            update_state(app_state=AppState.IDLE, current_file="Annulé", progress=0); return
        t2 = time.monotonic()
        logger.info(f"[SCAN] Cible collectée : {len(tgt_entries)} entrées en {t2-t1:.1f}s"
                    f" — {tgt_ignored} ignorée(s) par .zimaignore")

        # v3.13 — persiste la liste des ignorés pour l'onglet Détail
        total_ignored_real = src_ignored + tgt_ignored
        _write_ignored_files(ignored_sink,
                             capped=(len(ignored_sink) >= _IGNORED_LIST_CAP),
                             total=total_ignored_real)

        target_hashes = {}
        if _is_cloud:
            from rclone import fetch_remote_hashes, map_mount_to_remote
            if map_mount_to_remote(target) is None:
                logger.warning(f"[SCAN] Méthode 'cloud' mais cible {target} hors montage pCloud — repli sur 'secure'.")
                _is_cloud = False
                method = "secure"
            else:
                update_state(app_state=AppState.SCANNING, current_file="Empreintes cloud : préparation…", progress=0, fps=0, eta_seconds=0)
                def _hash_progress(done, total_dirs, name, n_hashes):
                    update_state(current_file=f"Empreintes cloud : {done}/{total_dirs} dossiers ({n_hashes} fichiers)", progress=int(done * 100 / total_dirs) if total_dirs else 0)
                _th0 = time.monotonic()
                try:
                    target_hashes = fetch_remote_hashes(target, "sha1", _hash_progress, _stop_event)
                except Exception as e:
                    logger.error(f"[SCAN] Empreintes cloud : {e}")
                    update_state(app_state=AppState.ERROR, error=f"Empreintes cloud : {e}")
                    return
                if _stop_event.is_set():
                    update_state(app_state=AppState.IDLE, current_file="Annulé", progress=0)
                    return
                logger.info(f"[SCAN] Empreintes cloud récupérées : {len(target_hashes)} fichiers en {time.monotonic()-_th0:.1f}s")
        all_paths = sorted(set(src_entries) | set(tgt_entries))
        total = len(all_paths)
        total_ignored = src_ignored + tgt_ignored
        update_state(app_state=AppState.COMPARING, total=total,
                     current_file="Comparaison en cours…",
                     ignored_count=total_ignored)
        logger.info(f"[SCAN] {total} entrées uniques à analyser"
                    f" (cumul ignored : {total_ignored})")

        SCAN_CSV.parent.mkdir(parents=True, exist_ok=True)
        with open(SCAN_CSV, "w", newline="", encoding="utf-8") as csvf:
            writer = csv.writer(csvf, delimiter=";")
            writer.writerow(CSV_COLUMNS)

            new = diff = deleted = identical = 0
            start = time.monotonic()
            bytes_to_copy = 0
            last_save = start

            for i, rel in enumerate(all_paths):
                if _stop_event.is_set():
                    logger.warning("[SCAN] Arrêt demandé par l'utilisateur")
                    update_state(app_state=AppState.IDLE, current_file="Annulé")
                    _cache_save(); return

                src_info = src_entries.get(rel)
                tgt_info = tgt_entries.get(rel)
                src_ex   = src_info is not None
                tgt_ex   = tgt_info is not None
                src_size, src_mtime, src_is_dir = src_info if src_ex else (0, 0.0, False)
                tgt_size, tgt_mtime, tgt_is_dir = tgt_info if tgt_ex else (0, 0.0, False)
                is_dir = src_is_dir if src_ex else tgt_is_dir

                if src_ex and not tgt_ex:
                    status, reason, sh, th = "new", "source_only", "", ""
                    new += 1
                    if not is_dir:
                        bytes_to_copy += src_size
                elif not src_ex and tgt_ex:
                    status, reason, sh, th = "deleted", "target_only", "", ""
                    deleted += 1
                elif is_dir:
                    status, reason, sh, th = "identical", "", "", ""
                    identical += 1
                elif _is_cloud:
                    status, reason, sh, th = compare_cloud(
                        src / rel, rel, src_size, tgt_size, src_mtime,
                        target_hashes, chunk_mb,
                    )
                    if status == "different":
                        diff += 1
                        bytes_to_copy += src_size
                    elif status == "identical":
                        identical += 1
                else:
                    status, reason, sh, th = compare_files(
                        src / rel, tgt / rel,
                        src_size, tgt_size, src_mtime, tgt_mtime,
                        method, chunk_mb,
                    )
                    if status == "different":
                        diff += 1
                        bytes_to_copy += src_size
                    elif status == "identical":
                        identical += 1

                writer.writerow([rel, src_size, src_mtime, tgt_size, tgt_mtime,
                                 is_dir, status, reason, sh, th])

                processed = i + 1
                elapsed = time.monotonic() - start
                fps = processed / elapsed if elapsed > 0 else 0
                eta = int((total - processed) / fps) if fps > 0 else 0

                if time.monotonic() - last_save > 30:
                    _cache_save(); last_save = time.monotonic()

                # NEW v3.4 : update des compteurs très souvent pour le mini-chart temps réel
                if processed % 100 == 0 or processed == total:
                    update_state(
                        processed=processed, total=total,
                        progress=int(processed / total * 100) if total else 0,
                        current_file=rel, fps=round(fps, 1), eta_seconds=eta,
                        new_count=new, different_count=diff,
                        deleted_count=deleted, identical_count=identical,
                        bytes_to_copy=bytes_to_copy,
                    )

        _cache_save()

        # NEW v3.4 : vérification de la source en fin de scan
        sig_end = _dir_signature(src)
        source_changed = (sig_end != sig_start)
        tsig_end = _dir_signature(tgt)
        target_changed = (tsig_end != tsig_start)
        source_warning = ""
        if source_changed:
            source_warning = (
                f"Le dossier source a été modifié pendant le scan "
                f"(mtime: {sig_start[0]:.0f} → {sig_end[0]:.0f}, "
                f"entries: {sig_start[1]} → {sig_end[1]}). "
                f"Le résultat reste utilisable mais peut être partiellement obsolète. "
                f"Relancez le scan pour la photo la plus à jour."
            )
            logger.warning(f"[SCAN] {source_warning}")

        cache_end = _cache_stats()
        new_entries = cache_end["entries"] - cache_start["entries"]
        elapsed_total = time.monotonic() - t0

        logger.info(
            f"[SCAN] Terminé en {elapsed_total:.1f}s — "
            f"new={new} diff={diff} deleted={deleted} identical={identical} "
            f"| cache : +{new_entries}, total {cache_end['entries']}"
        )

        update_state(
            app_state=AppState.IDLE, progress=100, current_file="",
            new_count=new, different_count=diff,
            deleted_count=deleted, identical_count=identical,
            bytes_to_copy=bytes_to_copy, scan_done=True,
            source_changed=source_changed, source_warning=source_warning,
            target_changed=target_changed, target_sig=f"{tsig_end[0]:.0f}:{tsig_end[1]}",
        )

    except Exception as e:
        logger.error(f"[SCAN] Erreur fatale: {e}", exc_info=True)
        update_state(app_state=AppState.ERROR, error=str(e))


def start_scan(source: str, target: str, method: str, chunk_mb: int = 4,
               clear_verification: bool = True, name_filter: str = "") -> bool:
    """Lance un scan en thread daemon.
    `clear_verification` : si False, on garde le champ sync_verified (utilisé
    par la vérification post-sync automatique pour ne pas écraser le résultat)."""
    from config import get_state
    state = get_state()
    if state["app_state"] not in (AppState.IDLE, AppState.ERROR):
        return False
    _stop_event.clear()

    payload = dict(source=source, target=target, method=method, scan_filter=name_filter, error="",
                   sync_done=0, sync_errors=0, sync_simulated=0)
    if clear_verification:
        payload.update(sync_verified="", sync_verified_msg="")
    update_state(**payload)

    t = threading.Thread(target=_run_scan, args=(source, target, method, chunk_mb, name_filter), daemon=True)
    t.start()
    return True


# ─────────────────────────────────────────────────────────────────────────
#  Contrôle ciblé niveau 3 — vérification secure sur les fichiers différents
# ─────────────────────────────────────────────────────────────────────────
from config import APP_DATA_ROOT

# Rapport du contrôle ciblé — fichier SÉPARÉ, n'écrase jamais scan_results.csv.
TARGETED_CSV = APP_DATA_ROOT / "targeted_check.csv"

# Nombre de tentatives de lecture avant de conclure à un échec (montage pCloud).
_TARGETED_READ_ATTEMPTS = 3


def _run_targeted_check(source: str, target: str, chunk_mb: int):
    """Contrôle ciblé : recalcule l'empreinte COMPLÈTE (niveau secure) des
    seuls fichiers 'different' du dernier scan, avec tentatives multiples de
    lecture. Écrit un rapport CSV séparé. Réutilise l'état de scan global.
    """
    from comparators import hash_full_xxh128_retry
    src_root = Path(source)
    tgt_root = Path(target)

    try:
        logger.info(f"[TARGETED] Démarrage — contrôle niveau 3 ciblé "
                    f"source={source} target={target}")

        # Liste des fichiers à vérifier : les 'different' du dernier scan.
        rep = diff_report()
        if not rep["available"]:
            update_state(app_state=AppState.ERROR,
                         error="Aucun scan disponible — lancez d'abord un scan.")
            return
        targets = [it["relative_path"] for it in rep["items"]]
        if not targets:
            update_state(app_state=AppState.ERROR,
                         error="Le dernier scan ne signale aucun fichier "
                               "différent — rien à contrôler.")
            return

        total = len(targets)
        update_state(app_state=AppState.COMPARING, total=total, processed=0,
                     progress=0, current_file="Contrôle approfondi…",
                     scan_done=False, fps=0, eta_seconds=0)

        rows = []
        n_identical = n_different = n_unreadable = 0
        t0 = time.time()

        for i, rel in enumerate(targets):
            if _stop_event.is_set():
                logger.warning("[TARGETED] Arrêt demandé")
                update_state(app_state=AppState.IDLE, current_file="Annulé",
                             progress=0)
                return

            src_p = src_root / rel
            tgt_p = tgt_root / rel
            verdict, reason = "", ""
            sh = th = ""
            s_try = t_try = 0

            # Tailles : une différence de taille tranche immédiatement.
            try:
                s_size = src_p.stat().st_size
            except OSError:
                s_size = -1
            try:
                t_size = tgt_p.stat().st_size
            except OSError:
                t_size = -1

            if s_size < 0 or t_size < 0:
                verdict, reason = "unreadable", "fichier inaccessible"
                n_unreadable += 1
            elif s_size != t_size:
                verdict, reason = "different", "taille différente"
                n_different += 1
            else:
                # Empreinte complète des deux côtés, avec tentatives multiples.
                sh, s_try = hash_full_xxh128_retry(
                    src_p, chunk_mb, _TARGETED_READ_ATTEMPTS)
                th, t_try = hash_full_xxh128_retry(
                    tgt_p, chunk_mb, _TARGETED_READ_ATTEMPTS)
                if sh == "error" or th == "error":
                    verdict = "unreadable"
                    reason = ("lecture source impossible" if sh == "error"
                              else "lecture cible impossible")
                    n_unreadable += 1
                elif sh == th:
                    verdict, reason = "identical", "empreinte complète identique"
                    n_identical += 1
                else:
                    verdict, reason = "different", "empreinte complète différente"
                    n_different += 1

            rows.append({
                "relative_path": rel,
                "verdict": verdict, "reason": reason,
                "source_size": s_size, "target_size": t_size,
                "source_hash": sh, "target_hash": th,
                "read_attempts": max(s_try, t_try),
            })

            done = i + 1
            elapsed = max(0.001, time.time() - t0)
            update_state(processed=done,
                         progress=int(done * 100 / total),
                         current_file=f"Contrôle : {rel}",
                         fps=done / elapsed,
                         eta_seconds=int((total - done) / (done / elapsed)))

        # Écriture du rapport CSV séparé.
        try:
            with open(TARGETED_CSV, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f, delimiter=";")
                w.writerow(["relative_path", "verdict", "reason",
                            "source_size", "target_size",
                            "source_hash", "target_hash", "read_attempts"])
                for r in rows:
                    w.writerow([r["relative_path"], r["verdict"], r["reason"],
                                r["source_size"], r["target_size"],
                                r["source_hash"], r["target_hash"],
                                r["read_attempts"]])
        except Exception as e:
            logger.error(f"[TARGETED] Écriture du rapport : {e}")

        logger.info(f"[TARGETED] Terminé — {total} fichier(s) contrôlé(s) : "
                    f"{n_identical} identique(s), {n_different} différent(s), "
                    f"{n_unreadable} illisible(s).")
        update_state(app_state=AppState.IDLE, progress=100, current_file="",
                     scan_done=True, fps=0, eta_seconds=0)

    except Exception as e:
        logger.error(f"[TARGETED] Erreur : {e}")
        update_state(app_state=AppState.ERROR, error=str(e))


def start_targeted_check(source: str, target: str, chunk_mb: int = 4) -> bool:
    """Lance le contrôle ciblé niveau 3 en thread daemon.
    Réutilise l'état de scan global (apparaît comme un scan dans l'UI)."""
    from config import get_state
    state = get_state()
    if state["app_state"] not in (AppState.IDLE, AppState.ERROR):
        return False
    _stop_event.clear()
    update_state(source=source, target=target, method="targeted_secure",
                 error="")
    t = threading.Thread(target=_run_targeted_check,
                         args=(source, target, chunk_mb), daemon=True)
    t.start()
    return True


def load_targeted_report() -> dict:
    """Charge le dernier rapport de contrôle ciblé (CSV séparé)."""
    if not TARGETED_CSV.exists():
        return {"available": False, "items": [], "by_verdict": {}}
    items = []
    by_verdict = {"identical": 0, "different": 0, "unreadable": 0}
    try:
        with open(TARGETED_CSV, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f, delimiter=";"):
                row["source_size"] = int(row.get("source_size") or 0)
                row["target_size"] = int(row.get("target_size") or 0)
                row["read_attempts"] = int(row.get("read_attempts") or 0)
                v = row.get("verdict", "")
                by_verdict[v] = by_verdict.get(v, 0) + 1
                items.append(row)
    except Exception as e:
        logger.error(f"[TARGETED] Lecture du rapport : {e}")
        return {"available": False, "items": [], "by_verdict": {}}
    try:
        generated_at = datetime.fromtimestamp(
            TARGETED_CSV.stat().st_mtime).isoformat(timespec="seconds")
    except OSError:
        generated_at = ""
    return {"available": True, "total": len(items),
            "by_verdict": by_verdict, "generated_at": generated_at,
            "items": items}


def load_scan_results() -> list:
    if not SCAN_CSV.exists():
        return []
    results = []
    try:
        with open(SCAN_CSV, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f, delimiter=";"):
                row["source_size"]  = int(row["source_size"] or 0)
                row["target_size"]  = int(row["target_size"] or 0)
                row["source_mtime"] = float(row["source_mtime"] or 0)
                row["target_mtime"] = float(row["target_mtime"] or 0)
                row["is_dir"]       = row["is_dir"] == "True"
                results.append(row)
    except Exception as e:
        logger.error(f"[SCAN] Lecture CSV: {e}")
    return results


def _diff_kind(row: dict) -> str:
    """Classe un fichier 'different' par TYPE d'écart, pour le rapport.

      - 'size'        : tailles source/cible différentes — vraie différence
                        certaine, indépendante de toute empreinte.
      - 'read_error'  : une empreinte vaut 'error' — la lecture (souvent côté
                        cible pCloud) a échoué ; le fichier n'a pas pu être
                        comparé. Probable faux positif à confirmer.
      - 'content'     : tailles égales, deux empreintes calculées mais
                        distinctes — divergence réelle de contenu dans la
                        zone analysée.
      - 'other'       : cas résiduel (pas d'empreinte, ni taille différente).
    """
    if (row.get("source_size") or 0) != (row.get("target_size") or 0):
        return "size"
    sh = (row.get("source_hash") or "").strip()
    th = (row.get("target_hash") or "").strip()
    if sh == "error" or th == "error":
        return "read_error"
    if sh and th and sh != th:
        return "content"
    return "other"


# Libellés humains des types d'écart (réutilisés par l'API et l'export).
DIFF_KIND_LABEL = {
    "size":       "Taille différente",
    "read_error": "Lecture cible impossible",
    "content":    "Contenu divergent",
    "other":      "Écart indéterminé",
}


def diff_report() -> dict:
    """Rapport des fichiers 'different' du dernier scan, classés par type
    d'écart. Exploite scan_results.csv — aucun recalcul.

    Retourne : { available, total_different, by_kind:{...}, items:[...] }
    où chaque item porte un champ 'kind' et 'kind_label'.
    """
    results = load_scan_results()
    if not results:
        return {"available": False, "total_different": 0,
                "by_kind": {}, "items": []}

    items = []
    by_kind = {"size": 0, "read_error": 0, "content": 0, "other": 0}
    for r in results:
        if r.get("is_dir"):
            continue
        if (r.get("status") or "").strip() != "different":
            continue
        kind = _diff_kind(r)
        by_kind[kind] = by_kind.get(kind, 0) + 1
        items.append({
            "relative_path": r.get("relative_path", ""),
            "source_size":   r.get("source_size", 0),
            "target_size":   r.get("target_size", 0),
            "reason":        r.get("reason", ""),
            "source_hash":   r.get("source_hash", ""),
            "target_hash":   r.get("target_hash", ""),
            "kind":          kind,
            "kind_label":    DIFF_KIND_LABEL.get(kind, kind),
        })
    return {
        "available": True,
        "total_different": len(items),
        "by_kind": by_kind,
        "items": items,
    }


def diff_report_csv() -> str:
    """Rapport des fichiers différents au format CSV (pour export/téléchargement)."""
    import io
    rep = diff_report()
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["relative_path", "kind", "kind_label",
                "source_size", "target_size", "reason",
                "source_hash", "target_hash"])
    for it in rep["items"]:
        w.writerow([it["relative_path"], it["kind"], it["kind_label"],
                    it["source_size"], it["target_size"], it["reason"],
                    it["source_hash"], it["target_hash"]])
    return buf.getvalue()


def compute_scan_stats() -> dict:
    results = load_scan_results()
    if not results:
        return {"total": 0}
    stats = {
        "total": len(results),
        "files": 0, "dirs": 0,
        "by_status": {"new": 0, "different": 0, "deleted": 0, "identical": 0, "error": 0},
        "by_extension": {},
        "size_buckets": {"<1KB": 0, "1KB-100KB": 0, "100KB-1MB": 0,
                          "1MB-100MB": 0, "100MB-1GB": 0, ">1GB": 0},
        "total_source_size": 0, "total_target_size": 0,
        "bytes_new": 0, "bytes_different": 0, "bytes_deleted": 0,
        "biggest_diffs": [], "biggest_files": [],
    }
    diff_c, big_c = [], []
    for r in results:
        is_dir = r["is_dir"]; st = r["status"]
        stats["by_status"][st] = stats["by_status"].get(st, 0) + 1
        if is_dir:
            stats["dirs"] += 1; continue
        stats["files"] += 1
        ssize, tsize = r["source_size"] or 0, r["target_size"] or 0
        stats["total_source_size"] += ssize
        stats["total_target_size"] += tsize
        name = r["relative_path"].rsplit("/", 1)[-1]
        if "." in name and not name.startswith("."):
            ext = name.rsplit(".", 1)[-1].lower()
            if len(ext) <= 8:
                stats["by_extension"][ext] = stats["by_extension"].get(ext, 0) + 1
        ref = ssize if ssize else tsize
        if ref < 1024:                  stats["size_buckets"]["<1KB"] += 1
        elif ref < 100*1024:            stats["size_buckets"]["1KB-100KB"] += 1
        elif ref < 1024*1024:           stats["size_buckets"]["100KB-1MB"] += 1
        elif ref < 100*1024*1024:       stats["size_buckets"]["1MB-100MB"] += 1
        elif ref < 1024*1024*1024:      stats["size_buckets"]["100MB-1GB"] += 1
        else:                           stats["size_buckets"][">1GB"] += 1
        if st == "new":         stats["bytes_new"]       += ssize
        elif st == "different": stats["bytes_different"] += ssize
        elif st == "deleted":   stats["bytes_deleted"]   += tsize
        if st in ("new", "different", "deleted"):
            diff_c.append((ref, r["relative_path"], st))
        big_c.append((ref, r["relative_path"], st))
    diff_c.sort(reverse=True); big_c.sort(reverse=True)
    stats["biggest_diffs"] = [{"size": s, "path": p, "status": st} for s, p, st in diff_c[:10]]
    stats["biggest_files"] = [{"size": s, "path": p, "status": st} for s, p, st in big_c[:10]]
    stats["by_extension"] = dict(sorted(stats["by_extension"].items(),
                                         key=lambda x: x[1], reverse=True)[:15])
    return stats


# ─────────────────────────────────────────────────────────────────────────
#  F4 — Playlist .m3u8 des albums à réparer (pour EZ CD).
# ─────────────────────────────────────────────────────────────────────────
_REPAIR_AUDIO_EXT = {".mp3", ".flac", ".m4a", ".wma", ".wav", ".ogg"}


def _album_of(rel: str) -> str:
    return rel.rsplit("/", 1)[0] if "/" in rel else ""


def repair_playlist(pc_root: str, kinds=("read_error", "content")) -> dict:
    """Playlist des albums à réparer, à partir de scan_results.csv.
    pc_root = racine vue par le PC (ex. 'Z:\\GoogleMusic'). Sortie = pc_root +
    '\\' + relative_path (en backslash). kinds = écarts 'à réparer'."""
    results = load_scan_results()
    kinds = tuple(kinds)
    problem_albums = set()
    for r in results:
        if r.get("is_dir"):
            continue
        if (r.get("status") or "").strip() != "different":
            continue
        if _diff_kind(r) in kinds:
            problem_albums.add(_album_of(r.get("relative_path", "")))
    rels = []
    for r in results:
        if r.get("is_dir"):
            continue
        if (r.get("status") or "").strip() == "deleted":
            continue
        rel = r.get("relative_path", "")
        if _album_of(rel) not in problem_albums:
            continue
        if os.path.splitext(rel)[1].lower() in _REPAIR_AUDIO_EXT:
            rels.append(rel)
    rels = sorted(set(rels))
    pcr = (pc_root or "").rstrip("/\\")
    def _to_pc(rel):
        tail = rel.replace("/", "\\")
        return f"{pcr}\\{tail}" if pcr else tail
    lines = ["#EXTM3U"] + [_to_pc(r) for r in rels]
    m3u8 = "\n".join(lines) + "\n"
    return {"album_count": len(problem_albums), "track_count": len(rels),
            "albums": sorted(problem_albums), "m3u8": m3u8}
