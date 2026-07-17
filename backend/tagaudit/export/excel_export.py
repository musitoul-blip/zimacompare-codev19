"""
export/excel_export.py - ZimaTAG Excel Exporter Premium
Export professionnel avec xlsxwriter.

Fonctionnalités:
  - Cockpit (Dashboard principal) avec KPI, charts, Health Score
  - Numéro de version affiché dans le Cockpit (auto-vérification du déploiement)
  - Bouton "🏠 Retour Cockpit" sur chaque onglet
  - DataViz avancée (pie, column, bar horizontal pour complétion)
  - Formatage conditionnel intelligent (data_bar, color_scale, icon_set)
  - Mode constant_memory désactivé par défaut (cf. corrections [21] ci-dessous)
  - Feuille _ChartData cachée pour alimenter les graphes proprement
  - Format dédié monospace pour colonnes de hash (MD5, SHA…)

Corrections appliquées :
  [21] Mode constant_memory : auparavant activé automatiquement quand
       le DataFrame principal dépassait 30 000 lignes. Or constant_memory
       impose à xlsxwriter d'écrire les cellules ligne par ligne, dans
       l'ordre croissant, sur TOUTES les feuilles du workbook (pas
       feuille par feuille). Or :
         - le Cockpit fait des écritures à positions calculées non
           strictement croissantes (entêtes réinsérés en `anchor_row - 1`
           après que d'autres écritures ont eu lieu plus bas) ;
         - la feuille cachée _ChartData reçoit des push successifs depuis
           plusieurs charts, à des plages de lignes calculées dynamiquement.
       Activer constant_memory dans ce contexte produit silencieusement un
       fichier corrompu ou tronqué pour les gros volumes (>30 K lignes).
       
       Nouvelle stratégie : constant_memory=False par défaut. Pour les
       très gros volumes où la mémoire est critique, possibilité d'activer
       explicitement via la variable d'environnement
       `ZIMATAG_EXCEL_CONSTANT_MEMORY=1`. Dans ce cas l'utilisateur est
       prévenu qu'un fichier corrompu peut résulter (warning loggé).
       
  [22] Garde anti-négatif : tous les `ws.insert_chart(anchor_row - 1, ...)`,
       `ws.write(anchor_row - 1, ...)` et `ws.set_row(anchor_row - 1, ...)`
       passent désormais par le helper interne `_anchor_above(anchor_row)`
       qui retourne `max(0, anchor_row - 1)`. Évite les écritures à des
       indices négatifs si un appelant fournit accidentellement
       `anchor_row=0`. En pratique non observé, mais coûte zéro.
"""
import os

# IMPORTANT : cette version apparaît dans le Cockpit. Incrémenter à chaque
# modification pour vérifier d'un coup d'œil quelle version du code a
# effectivement généré un fichier Excel.
EXPORTER_VERSION = "2.5.1"
EXPORTER_BUILD_DATE = "2026-04-25"
import pandas as pd
import numpy as np
import tempfile
import shutil
from pathlib import Path
from datetime import datetime
from typing import Dict, Optional, List, Tuple, Any
from core import logger, config, db
from core import audit_registry  # T10 Lot F2

# [LOT v20-5d] Colonnes de la table SQLite tracks, dans l'ordre du CSV (id
# exclu). Duplique la liste deja presente dans engine/scanner.py/
# fingerprint_sqlite.py/audit_engine.py -- dette de consolidation assumee,
# notee pour le nettoyage du LOT v20-7. Reutilisee par html_export.py.
SQLITE_COLUMNS = [
    'filepath', 'filename', 'extension', 'directory', 'parent_folder',
    'size_mb', 'modified_date', 'file_md5', 'title', 'artist', 'album', 'albumartist',
    'composer', 'genre', 'year', 'track', 'tracktotal', 'disc', 'disctotal',
    'encoder', 'duration', 'duration_seconds', 'bitrate', 'samplerate',
    'channels', 'bitdepth', 'codec', 'id3_version', 'has_cover',
    'cover_size', 'cover_format', 'cover_width', 'cover_height',
    'cover_md5', 'cover_valid', 'cover_error', 'cover_count', 'error'
]


class ExcelExporter:
    """Exporteur Excel haut de gamme avec xlsxwriter."""

    # ------------------------------------------------------------------
    # Charte graphique
    # ------------------------------------------------------------------
    COLORS = {
        'primary':   '#1F4E79',  # Bleu profond
        'secondary': '#2E86AB',  # Bleu clair
        'accent':    '#5BA4C6',  # Bleu accent
        'success':   '#28A745',  # Vert
        'warning':   '#FFC107',  # Orange
        'danger':    '#DC3545',  # Rouge
        'light':     '#F8F9FA',  # Gris très clair
        'zebra':     '#F2F2F2',  # Gris zebra
        'dark':      '#343A40',  # Gris foncé
        'white':     '#FFFFFF',
    }

    # Nom du Cockpit (doit rester ≤ 31 chars). Utilisé par les liens retour.
    COCKPIT_SHEET = '🎯 Cockpit'

    # Feuille cachée alimentant les graphiques du cockpit
    CHART_DATA_SHEET = '_ChartData'

    # Colonnes considérées comme des hash (affichage monospace + largeur forcée)
    HASH_COLUMNS = {'file_md5', 'md5', 'file_sha1', 'sha1', 'file_sha256', 'sha256'}
    
    # Seuils Bluesound Node : pochettes optimales à afficher sur le streamer
    # Au-delà de ces limites, la pochette risque de ne pas s'afficher correctement.
    BLUESOUND_MAX_WIDTH = 1200      # pixels
    BLUESOUND_MAX_HEIGHT = 1200     # pixels
    BLUESOUND_MAX_SIZE_KB = 700    # kilo-octets (F20 : recalibrage poids seul, plancher reel 741 Ko)

    # Groupes d'onglets thématiques
    _SHEET_GROUPS_FALLBACK = {  # T10 Lot F2 (base = source primaire)
        'cockpit': [
            ('🎯 Cockpit', 'cockpit'),
        ],
        'kpi': [
            ('📊 KPI Global', 'kpi_dashboard'),
            ('📅 KPI Années', 'kpi_years'),
            ('🎵 KPI Genres', 'kpi_genres'),
            ('👤 KPI Artistes', 'kpi_albumartists'),
        ],
        'qualite': [
            ('🎧 Qualité Audio', 'quality_analysis'),
            ('🔀 Bitrate mixte/album', 'bitrate_mixed_album'),
            ('🔊 Incohér. Samplerate', 'samplerate_inconsistency'),
            ('🆔 Version ID3 mixte', 'id3_version_inconsistency'),
            ('📀 Homogénéité Codec', 'codec_homogeneity'),
            ('⏱️ Durée nulle', 'duration_zero'),
        ],
        'integrite': [
            ('📦 Albums Incomplets', 'incomplete_albums'),
            ('🔢 Trous Numérotation', 'track_gaps'),
            ('📝 Écarts Album', 'album_gaps'),
            ('📋 Écarts Détaillés', 'album_gaps_detailed'),
        ],
        'metadonnees': [
            ('🏷️ Tags Manquants', 'missing_metadata'),
            ('🔣 Mojibake', 'mojibake'),
            ('🚫 Sans Genre', 'missing_genre_albums'),
            ('📆 Sans Année', 'missing_year_albums'),
            ('⚠️ Années Invalides', 'invalid_year_format'),
            ('👥 Cohér. Album Artist', 'albumartist_consistency'),
            ('💿 Cohér. Nom Album', 'album_name_consistency'),
            ('🎭 Incohér. Genre', 'genre_inconsistency'),
            ('✏️ Typo AlbumArtist', 'albumartist_typo'),
            ('📂 Dossier ≠ AlbumArtist', 'folder_artist_mismatch'),
        ],
        'doublons': [
            ('🔍 Doublons MD5', 'duplicates_md5'),
            ('🎤 Doublons Titre', 'duplicates_artist_title'),
        ],
        'casse': [
            ('🔠 Casse AlbumArtist', 'case_inconsistency_artist'),
            ('🔡 Casse Albums', 'case_inconsistency_album'),
            ('🔤 Casse Genres', 'case_inconsistency_genre'),
            ('📋 Casse AlbumArtist-Album', 'case_by_artist_album'),
        ],
        'images': [
            ('🎨 Covers Non-Uniformes', 'cover_non_uniform'),
            ('🚫 Pochettes non-JPG', 'covers_non_jpg'),
            ('❌ Pochettes corrompues', 'covers_invalid'),
            ('🔍 Pochettes trop petites', 'covers_too_small'),
            ('🖼️ Images multiples', 'multiple_covers'),
        ],
        'donnees': [
            ('📁 Données Complètes', 'music_tags'),
            ('🪟 Chemins Windows', 'windows_path_issues'),
        ],
        # T10 Lot C: onglet Informations (signal informatif volumineux, hors defauts)
        'informations': [
            ('👤 Artist ≠ AlbumArtist', 'albumartist_vs_artist'),
            ('⚡ Anomalies Bitrate', 'bitrate_anomalies'),
            ('🖼️ Taille Pochettes', 'cover_size'),
            ('📺 Pochettes > Bluesound', 'covers_bluesound_oversized'),
            ('📈 Stats Genres', 'genre_stats'),
            ('📅 Incohér. Année', 'year_inconsistency'),
        ],
    }

    # Pondérations utilisées pour calculer le Health Score (clé = data_key)
    # Plus le poids est élevé, plus l'impact négatif sur le score est fort.
    _HEALTH_WEIGHTS_FALLBACK = {  # T10 Lot F2 (base = source primaire)
        'duplicates_md5': 3.0,
        'duplicates_artist_title': 0.0,
        'missing_metadata': 2.5,
        'mojibake': 0.8,
        'incomplete_albums': 2.0,
        'track_gaps': 1.5,
        'bitrate_anomalies': 0.0,  # T10 Lot A (etait 1.5)
        'bitrate_mixed_album': 0.0,
        'samplerate_inconsistency': 1.0,
        'id3_version_inconsistency': 0.0,
        'albumartist_typo': 0.0,
        'folder_artist_mismatch': 0.0,
        'invalid_year_format': 1.5,
        'year_inconsistency': 0.0,  # T10 Lot A (etait 1.0)
        'genre_inconsistency': 0.8,
        'albumartist_consistency': 1.2,
        'album_name_consistency': 0.0,  # T10 Lot A (etait 1.2)
        'albumartist_vs_artist': 0.0,
        'missing_genre_albums': 1.0,
        'missing_year_albums': 1.0,
        'case_inconsistency_artist': 0.5,
        'case_inconsistency_album': 0.0,  # T10 Lot A (etait 0.5)
        'case_inconsistency_genre': 0.5,
        'cover_size': 0.0,  # T10 Lot A (etait 0.3)
        'cover_non_uniform': 0.3,
        'multiple_covers': 0.3,
    }

    def __init__(self):
        self.workbook = None
        self.formats: Dict[str, Any] = {}
        self.audit_results: Dict[str, Any] = {}
        self.df_main: Optional[pd.DataFrame] = None
        self._chart_data_row = 0  # curseur d'écriture dans la feuille cachée
        self._chart_data_ws = None
        # Stocke les plages à utiliser pour les charts, nom -> (col_cat, col_val, start_row, end_row)
        self._chart_ranges: Dict[str, Tuple[str, str, int, int]] = {}
        self._sheet_groups_cache = None  # T10 Lot F2
        self._health_weights_cache = None  # T10 Lot F2

    # ------------------------------------------------------------------
    # T10 Lot F2 : SHEET_GROUPS / HEALTH_WEIGHTS lus depuis audit_registry
    # (source primaire). Fallback sur les constantes _*_FALLBACK si la base
    # est indisponible. Cache par instance (1 lecture par generation).
    @property
    def SHEET_GROUPS(self):
        if self._sheet_groups_cache is None:
            try:
                audit_registry.init_and_seed()
                sg = audit_registry.get_sheet_groups()
                self._sheet_groups_cache = sg if sg else self._SHEET_GROUPS_FALLBACK
            except Exception:
                self._sheet_groups_cache = self._SHEET_GROUPS_FALLBACK
        return self._sheet_groups_cache

    @property
    def HEALTH_WEIGHTS(self):
        if self._health_weights_cache is None:
            try:
                audit_registry.init_and_seed()
                hw = audit_registry.get_health_weights()
                self._health_weights_cache = hw if hw else self._HEALTH_WEIGHTS_FALLBACK
            except Exception:
                self._health_weights_cache = self._HEALTH_WEIGHTS_FALLBACK
        return self._health_weights_cache

    # ==================================================================
    # API publique
    # ==================================================================
    def export(self, output_path: Optional[Path] = None) -> Path:
        """Exporte les données vers Excel avec écriture atomique.
        
        Tous les artefacts (xlsx, .mta, .m3u, README) sont regroupés dans
        un sous-répertoire horodaté : ZimaTAG_Audit_YYYYMMDD_HHMMSS/
        """
        import xlsxwriter
        
        if output_path is None:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            # Sous-répertoire horodaté qui contiendra tous les artefacts
            export_dir = config.DATA_DIR / f"ZimaTAG_Audit_{timestamp}"
            output_path = export_dir / f"ZimaTAG_Audit_{timestamp}.xlsx"
        
        # Crée le dossier parent (le sous-répertoire horodaté)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # --- Chargement depuis SQLite (LOT v20-5d) ---
        db_path = Path(db.DB_PATH)
        if not db_path.exists():
            logger.error("Base master_scan.db introuvable")
            raise FileNotFoundError(f"Base non trouvée: {db_path}")

        conn = db.connect()
        try:
            self.df_main = pd.read_sql(
                "SELECT " + ",".join(SQLITE_COLUMNS) + " FROM tracks ORDER BY id",
                conn,
            )
        finally:
            conn.close()
        logger.info(f"Chargé {len(self.df_main)} lignes depuis {db_path}")

        # --- Audits ---
        from audit import AuditEngine
        engine = AuditEngine(self.df_main)
        self.audit_results = engine.run_all_audits()
        self.audit_results['music_tags'] = self.df_main

        # --- Enrichissement des résultats d'audit ---
        # Certains modules d'audit ne remontent pas toutes les colonnes utiles
        # (typiquement `file_md5` absent du DataFrame des doublons MD5).
        # On corrige ça ici en joignant avec df_main quand nécessaire.
        self._enrich_audit_results()

        # --- Écriture atomique via tmp file ---
        with tempfile.NamedTemporaryFile(delete=False, suffix='.xlsx') as tmp:
            tmp_path = Path(tmp.name)

        try:
            # [21] constant_memory n'est plus activé automatiquement.
            # Raison : le Cockpit et la feuille _ChartData font des écritures
            # à des positions arbitraires (entêtes en anchor_row-1, push de
            # chart_data depuis plusieurs charts). constant_memory exigerait
            # un ordre d'écriture strictement séquentiel par ligne sur TOUTES
            # les feuilles du workbook — incompatible avec ce design.
            # 
            # Pour les très gros volumes où la RAM est critique, l'utilisateur
            # peut activer explicitement le mode via la variable d'env
            # ZIMATAG_EXCEL_CONSTANT_MEMORY=1 (warning loggé).
            cm_env = os.environ.get('ZIMATAG_EXCEL_CONSTANT_MEMORY', '').strip().lower()
            use_constant_memory = cm_env in ('1', 'true', 'yes', 'on')
            if use_constant_memory:
                logger.warning(
                    f"[EXPORT] constant_memory activé via env "
                    f"ZIMATAG_EXCEL_CONSTANT_MEMORY=1 ({len(self.df_main)} lignes). "
                    f"ATTENTION : le Cockpit et _ChartData peuvent produire "
                    f"un fichier Excel corrompu en raison d'écritures "
                    f"non séquentielles."
                )
            elif len(self.df_main) > 30000:
                logger.info(
                    f"[EXPORT] {len(self.df_main)} lignes — mode standard "
                    f"(constant_memory volontairement désactivé pour "
                    f"compatibilité Cockpit/_ChartData)."
                )

            # IMPORTANT :
            # - strings_to_numbers=False : évite les conversions non voulues
            #   (notamment sur les hash MD5 composés de chiffres, les années
            #   stockées comme strings, les codes numériques, etc.)
            # - nan_inf_to_errors=True : protège contre les #NUM! invisibles
            self.workbook = xlsxwriter.Workbook(str(tmp_path), {
                'strings_to_numbers': False,
                'strings_to_urls': False,
                'nan_inf_to_errors': True,
                'default_date_format': 'yyyy-mm-dd',
                'constant_memory': use_constant_memory,
            })

            # --- Initialisation ---
            self._init_formats()

            # IMPORTANT : ordre de création des feuilles
            # ----------------------------------------------------------
            # La feuille cachée _ChartData doit être créée APRÈS le Cockpit
            # sinon xlsxwriter la marque comme "first_sheet" et Excel l'ouvre
            # par défaut à la réouverture du fichier, rendant `hide()` inefficace.
            # On la crée donc ici en avance (add_worksheet) mais on ne lui
            # écrira des données que plus tard. xlsxwriter accepte les
            # références 'forward' vers une feuille pas encore peuplée.
            # Alternative : on la crée vraiment APRÈS le Cockpit.
            # ----------------------------------------------------------

            # Cockpit (dashboard principal) -- créé EN PREMIER pour être
            # la feuille active à l'ouverture du fichier
            try:
                # On crée la feuille maintenant pour qu'elle soit la 1ʳᵉ,
                # mais on diffère la création du contenu après _ChartData
                # pour avoir les références correctes.
                self._cockpit_ws = self.workbook.add_worksheet(self.COCKPIT_SHEET)
                self._cockpit_ws.set_tab_color(self.COLORS['primary'])
            except Exception as e:
                logger.error(f"[EXPORT] Erreur création Cockpit: {e}")
                self._cockpit_ws = None

            # Feuille cachée _ChartData créée APRÈS le Cockpit (donc en
            # position 2 dans l'ordre de création) -> hide() est effectif.
            self._chart_data_ws = self.workbook.add_worksheet(self.CHART_DATA_SHEET)
            self._chart_data_ws.hide()

            # On peuple maintenant le Cockpit (après que _ChartData existe,
            # toutes les références '_ChartData'!... sont valides)
            if self._cockpit_ws is not None:
                try:
                    self._create_cockpit()
                except Exception as e:
                    logger.error(f"[EXPORT] Erreur remplissage Cockpit: {e}")

            # Force le Cockpit comme feuille active à l'ouverture
            if self._cockpit_ws is not None:
                self._cockpit_ws.activate()
                self._cockpit_ws.set_first_sheet()

            # Onglets de données
            for group_name, sheets in self.SHEET_GROUPS.items():
                if group_name == 'cockpit':
                    continue
                for sheet_name, data_key in sheets:
                    try:
                        self._create_data_sheet(sheet_name, data_key, group_name)
                    except Exception as e:
                        logger.error(
                            f"[EXPORT] Erreur création feuille '{sheet_name}' "
                            f"(key={data_key}): {e}"
                        )

            self.workbook.close()
            shutil.move(str(tmp_path), str(output_path))
            logger.info(f"Export Excel créé: {output_path}")

            # --- Génération des fichiers compagnons pour pochettes non-JPG ---
            try:
                self._generate_mp3tag_companions(output_path)
            except Exception as e:
                logger.error(f"[EXPORT] Erreur génération fichiers mp3tag: {e}")

        except Exception:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
            raise

        return output_path

    # ==================================================================
    # Enrichissement des résultats d'audit
    # ==================================================================
    def _enrich_audit_results(self):
        """Complète / remplace certains DataFrames d'audit.

        Actions effectuées :
          1. `duplicates_md5` est REGÉNÉRÉ à partir de df_main. Il ne
             contient QUE les fichiers dont file_md5 est identique à au
             moins un autre fichier. C'est la sémantique attendue par
             l'utilisateur.
          2. `duplicates_artist_title` est conservé tel que fourni par le
             module audit (il sert à détecter les doublons logiques :
             même titre + même artiste mais pas forcément même binaire).
             On y attache file_md5 quand il est absent, pour permettre
             de recouper avec les doublons binaires.
          3. `covers_non_jpg` est CRÉÉ à partir de df_main : liste de
             tous les titres dont la pochette n'est pas au format JPG.
        """
        if self.df_main is None or self.df_main.empty:
            return

        # --- 1. Doublons MD5 STRICTS (vrais doublons binaires) ---
        self.audit_results['duplicates_md5'] = self._compute_md5_duplicates()

        # --- 2. Enrichir duplicates_artist_title avec file_md5 pour recoupement ---
        df_dup2 = self.audit_results.get('duplicates_artist_title')
        if isinstance(df_dup2, pd.DataFrame) and not df_dup2.empty:
            if 'file_md5' not in df_dup2.columns:
                self.audit_results['duplicates_artist_title'] = \
                    self._attach_md5_column(df_dup2)

        # --- 3. Pochettes non-JPG ---
        self.audit_results['covers_non_jpg'] = self._compute_covers_non_jpg()
        
        # --- 4. Pochettes > limite Bluesound Node (poids > 700 Ko) ---
        self.audit_results['covers_bluesound_oversized'] = \
            self._compute_covers_bluesound_oversized()

    def _compute_md5_duplicates(self) -> pd.DataFrame:
        """Calcule les vrais doublons binaires à partir de df_main.

        Retourne un DataFrame contenant uniquement les fichiers dont le
        file_md5 apparaît au moins 2 fois. Trié par MD5 pour que les
        doublons soient groupés visuellement.
        
        Colonnes : file_md5, file_path, filename, size_mb, artist, album,
        title, extension, has_cover, year (si disponibles).
        """
        df = self.df_main
        if 'file_md5' not in df.columns:
            return pd.DataFrame()

        # On travaille uniquement sur les lignes qui ont un MD5
        mask_md5 = df['file_md5'].notna() & \
            (df['file_md5'].astype(str).str.strip() != '')
        df_with_md5 = df[mask_md5]
        if df_with_md5.empty:
            return pd.DataFrame()

        # Identifie les MD5 qui apparaissent au moins 2 fois
        duplicated_mask = df_with_md5['file_md5'].duplicated(keep=False)
        df_dups = df_with_md5[duplicated_mask].copy()
        if df_dups.empty:
            return pd.DataFrame()

        # Colonnes souhaitées, dans l'ordre. file_md5 en premier.
        preferred_cols = [
            'file_md5', 'file_path', 'filepath', 'filename', 'size_mb',
            'artist', 'album', 'title', 'extension', 'has_cover',
            'year', 'bitrate', 'duration',
        ]
        available = [c for c in preferred_cols if c in df_dups.columns]
        # Ajoute les autres colonnes utiles non listées (mais en queue)
        rest = [c for c in df_dups.columns if c not in available]
        df_dups = df_dups[available + rest]

        # Tri par MD5 pour grouper les doublons, puis par path
        sort_path_col = 'file_path' if 'file_path' in df_dups.columns \
            else ('filepath' if 'filepath' in df_dups.columns else None)
        sort_cols = ['file_md5']
        if sort_path_col:
            sort_cols.append(sort_path_col)
        df_dups = df_dups.sort_values(by=sort_cols).reset_index(drop=True)

        logger.info(
            f"[EXPORT] Doublons MD5 stricts : {len(df_dups)} fichiers "
            f"sur {df_dups['file_md5'].nunique()} groupes"
        )
        return df_dups

    def _compute_covers_non_jpg(self) -> pd.DataFrame:
        """Retourne la liste des titres dont la pochette n'est pas en JPG.

        Seuls les fichiers qui ONT une pochette sont inclus (on ne veut
        pas d'une liste avec les fichiers sans pochette ici, qui est
        une anomalie différente gérée ailleurs).
        """
        df = self.df_main
        if 'cover_format' not in df.columns:
            # Pas de colonne cover_format -> scanner pas encore mis à jour
            return pd.DataFrame()

        # Filtre : cover présente ET format non JPG
        has_cover_mask = (df['has_cover'] == 'Yes') \
            if 'has_cover' in df.columns else pd.Series(True, index=df.index)

        # Normalisation pour la comparaison (tolère image/jpeg, JPEG, .jpg...)
        def _is_jpg(val):
            if val is None:
                return False
            s = str(val).strip().lower()
            if not s or s in ('nan', 'none', 'null'):
                return False
            if '/' in s:
                s = s.split('/', 1)[1]
            s = s.lstrip('.')
            return s in ('jpg', 'jpeg', 'pjpeg')

        cover_fmt = df['cover_format']
        is_jpg_mask = cover_fmt.apply(_is_jpg)
        # Non-JPG = a une cover non vide ET n'est pas du JPG
        has_cover_fmt_mask = cover_fmt.notna() & \
            (cover_fmt.astype(str).str.strip() != '')
        non_jpg_mask = has_cover_mask & has_cover_fmt_mask & ~is_jpg_mask

        if not non_jpg_mask.any():
            return pd.DataFrame()

        df_non_jpg = df[non_jpg_mask].copy()

        # Colonnes utiles pour identifier les titres + générer le script mp3tag
        preferred_cols = [
            'file_path', 'filepath', 'filename', 'artist', 'album',
            'title', 'track', 'year', 'cover_format', 'cover_size',
            'extension',
        ]
        available = [c for c in preferred_cols if c in df_non_jpg.columns]
        df_non_jpg = df_non_jpg[available]

        # Tri : artist > album > track pour lecture naturelle
        sort_cols = [c for c in ('artist', 'album', 'track', 'title')
                     if c in df_non_jpg.columns]
        if sort_cols:
            df_non_jpg = df_non_jpg.sort_values(by=sort_cols).reset_index(drop=True)

        logger.info(f"[EXPORT] Pochettes non-JPG : {len(df_non_jpg)} fichiers")
        return df_non_jpg
    
    def _compute_covers_bluesound_oversized(self) -> pd.DataFrame:
        """Pochettes dont le POIDS depasse la limite Bluesound Node (> 700 Ko).
        F20 (recalibrage) : seul le poids predit un probleme d'affichage ; la
        dimension en pixels n'est plus un critere (une pochette 3000 px / 369 Ko
        reste saine). Plancher des cas reels = 741 Ko ; seuil = 700 Ko. Ecart
        volontaire vs la reference ZimaTAG (qui garde l'ancienne regle px OU poids).
        Necessite cover_size dans df_main ; sinon DataFrame vide sans erreur.
        """
        df = self.df_main
        if 'cover_size' not in df.columns:
            return pd.DataFrame()
        # T10 Lot I2 : seuil editable (audit_params), fallback = constante de classe
        max_kb = audit_registry.get_audit_param('bluesound_max_kb', self.BLUESOUND_MAX_SIZE_KB)
        def _to_int_series(s):
            return pd.to_numeric(s, errors='coerce').fillna(0).astype(int)
        has_cover_mask = (df['has_cover'] == 'Yes') \
            if 'has_cover' in df.columns else pd.Series(True, index=df.index)
        size_bytes = _to_int_series(df['cover_size'])
        oversize_size_mask = size_bytes > (max_kb * 1024)  # T10 Lot I2
        combined_mask = has_cover_mask & oversize_size_mask
        if not combined_mask.any():
            return pd.DataFrame()
        df_over = df[combined_mask].copy()
        def _compute_reason(row):
            try:
                s_bytes = int(pd.to_numeric(row.get('cover_size'), errors='coerce') or 0)
            except (TypeError, ValueError):
                s_bytes = 0
            return '%.0f Ko > %d Ko' % (s_bytes / 1024.0, max_kb)  # T10 Lot I2
        df_over['raison'] = df_over.apply(_compute_reason, axis=1)
        df_over['cover_size_kb'] = (_to_int_series(df_over['cover_size']) / 1024.0).round(1)
        preferred_cols = [
            'file_path', 'filepath', 'raison',
            'cover_width', 'cover_height', 'cover_size_kb', 'cover_size',
            'cover_format', 'filename', 'artist', 'album', 'title',
            'track', 'year', 'extension',
        ]
        available = [c for c in preferred_cols if c in df_over.columns]
        df_over = df_over[available]
        if 'cover_size_kb' in df_over.columns:
            df_over = df_over.sort_values(by='cover_size_kb', ascending=False).reset_index(drop=True)
        logger.info(
            "[EXPORT] Pochettes hors-normes Bluesound : "
            "%d fichiers (seuil : poids > %d Ko)"
            % (len(df_over), max_kb)  # T10 Lot I2
        )
        return df_over

    def _attach_md5_column(self, df: pd.DataFrame) -> pd.DataFrame:
        """Attache la colonne file_md5 au DataFrame via une jointure sur le path.

        Place file_md5 en 1ʳᵉ position. Utilisé pour enrichir
        duplicates_artist_title afin de permettre de voir si un doublon
        logique est aussi un vrai doublon binaire.
        """
        if 'file_md5' in df.columns:
            non_null = df['file_md5'].notna() & \
                (df['file_md5'].astype(str).str.strip() != '')
            if non_null.any():
                return self._move_column_first(df, 'file_md5')
            df = df.drop(columns=['file_md5'])

        join_candidates = ['file_path', 'filepath', 'path', 'filename']
        df_key = next((c for c in join_candidates if c in df.columns), None)
        main_key = None
        if 'file_md5' in self.df_main.columns:
            main_key = next((c for c in join_candidates
                             if c in self.df_main.columns), None)

        if df_key is None or main_key is None:
            return df

        md5_map = self.df_main[[main_key, 'file_md5']].dropna(subset=[main_key])
        md5_map = md5_map.drop_duplicates(subset=[main_key])
        merged = df.merge(
            md5_map.rename(columns={main_key: df_key}),
            on=df_key, how='left',
        )
        return self._move_column_first(merged, 'file_md5')

    @staticmethod
    def _move_column_first(df: pd.DataFrame, col_name: str) -> pd.DataFrame:
        """Déplace une colonne en première position du DataFrame."""
        if col_name not in df.columns:
            return df
        cols = [col_name] + [c for c in df.columns if c != col_name]
        return df[cols]

    # ==================================================================
    # Génération des fichiers compagnons mp3tag
    # ==================================================================
    @staticmethod
    def _get_path_mappings() -> "Dict[str, str]":
        """Récupère le mapping Linux→Windows depuis la config.
        
        Sources, par ordre de priorité croissante :
          1. config.PATH_MAPPINGS (défini dans core/config.py ou settings.json)
          2. st.session_state['path_mappings'] (override UI temporaire)
        
        Retourne un dict (vide si aucun mapping configuré).
        """
        mappings = {}
        
        # 1. Config statique / persistée
        cfg_mappings = getattr(config, 'PATH_MAPPINGS', None)
        if isinstance(cfg_mappings, dict):
            mappings.update(cfg_mappings)
        
        # 2. Override UI (Streamlit session_state)
        try:
            import streamlit as st
            ui_mappings = st.session_state.get('path_mappings')
            if isinstance(ui_mappings, dict):
                mappings.update(ui_mappings)
        except Exception:
            pass
        
        return mappings

    @classmethod
    def _translate_path_for_windows(cls, path: str) -> str:
        """Traduit un chemin Linux vers son équivalent Windows.
        
        Délègue à config.to_windows_path si disponible (ajouté dans
        core/config.py v2+). Sinon, applique localement le mapping via
        _get_path_mappings() comme fallback.
        
        Si le chemin est déjà Windows (ex: T:\\...), il est retourné
        inchangé.
        """
        if not isinstance(path, str) or not path:
            return path
        # Déjà un chemin Windows ? (lettre + ':\\') on ne touche pas
        if len(path) >= 3 and path[1:3] == ':\\':
            return path
        
        # Voie principale : déléguer à config (source de vérité unique)
        to_win = getattr(config, 'to_windows_path', None)
        if callable(to_win):
            try:
                return to_win(path)
            except Exception:
                pass
        
        # Fallback : application manuelle du mapping (tri longueur desc)
        mappings = cls._get_path_mappings()
        for linux_prefix in sorted(mappings.keys(), key=len, reverse=True):
            if path.startswith(linux_prefix):
                win_prefix = mappings[linux_prefix].rstrip('\\').rstrip('/')
                tail = path[len(linux_prefix):].replace('/', '\\')
                if not tail.startswith('\\'):
                    tail = '\\' + tail
                return win_prefix + tail
        
        # Aucun mapping : convertit juste les séparateurs
        return path.replace('/', '\\')

    def _generate_mp3tag_companions(self, xlsx_path: Path):
        """Génère les fichiers compagnons mp3tag à côté du fichier Excel.
        
        Pour chaque type de correction détecté, produit un couple :
          - *.mta : action mp3tag pré-configurée
          - *.m3u : playlist des fichiers concernés (chemins Windows)
        
        Plus un fichier README.txt récapitulant tout.
        
        Corrections gérées :
          1. Pochettes non-JPG → conversion en JPEG 500px (Adjust Cover)
          2. Pochettes > Bluesound : reduire le poids a <= 700 Ko (Adjust Cover)
        
        Les fichiers ne sont créés que si la correction s'applique
        (DataFrame non vide). Sinon : rien (pas de bruit inutile).
        
        Spec format .mta pour Adjust Cover (T=17) :
          1=0|1|2  : format de sortie (0=Original, 1=JPEG, 2=PNG)
          2=pixels : taille maximum
          3=0-100  : qualité (100 = meilleure)
        """
        # Seuils Bluesound dynamiques (audit_params, fallback constantes)
        from core import audit_registry as _arz
        _bs_kb = int(_arz.get_audit_param('bluesound_max_kb', self.BLUESOUND_MAX_SIZE_KB))
        _bs_px = int(_arz.get_audit_param('bluesound_resize_px', 600))
        base = xlsx_path.with_suffix('')  # Ex: .../ZimaTAG_Audit_20260422_114358
        generated = []  # Liste des corrections effectivement générées
        
        # --- Correction 1 : Pochettes non-JPG ---
        df_non_jpg = self.audit_results.get('covers_non_jpg')
        if isinstance(df_non_jpg, pd.DataFrame) and not df_non_jpg.empty:
            gen = self._write_fix_pair(
                df=df_non_jpg,
                base_path=base,
                suffix='FixCovers_NonJpg',
                mta_params={'1': 1, '2': 500, '3': 100},
                title='Pochettes non-JPG → JPEG 500px',
                description=(
                    f'Conversion de {len(df_non_jpg):,} pochette(s) '
                    f'au format JPEG, max 500×500 px, qualité 100.'
                ),
            )
            if gen:
                generated.append(gen)
        
        # --- Correction 2 : Pochettes hors-normes Bluesound Node ---
        df_bluesound = self.audit_results.get('covers_bluesound_oversized')
        if isinstance(df_bluesound, pd.DataFrame) and not df_bluesound.empty:
            gen = self._write_fix_pair(
                df=df_bluesound,
                base_path=base,
                suffix='FixCovers_Bluesound',
                mta_params={'1': 1, '2': _bs_px, '3': 100},
                title=f'Pochettes > Bluesound : poids <= {_bs_kb} Ko',
                description=(
                    f'Redimensionnement de {len(df_bluesound):,} pochette(s) '
                    f'à {_bs_px}×{_bs_px} px maximum, format JPEG, qualité 100. '
                    f'Cible : streamer Bluesound Node.'
                ),
            )
            if gen:
                generated.append(gen)
        
        # --- Correction 3 : Pochettes corrompues (liste seule, sans action) ---
        df_invalid = self.audit_results.get('covers_invalid')
        if isinstance(df_invalid, pd.DataFrame) and not df_invalid.empty:
            gen = self._write_fix_pair(
                df=df_invalid,
                base_path=base,
                suffix='ListCovers_Invalid',
                mta_params=None,
                title='Pochettes corrompues (a inspecter)',
                description=(
                    f'{len(df_invalid):,} pochette(s) illisible(s) (Pillow). '
                    f'Liste seule : ouvre le M3U dans mp3tag et inspecte/repare '
                    f'chaque fichier manuellement (aucune action automatique).'
                ),
            )
            if gen:
                generated.append(gen)

        # Si rien à faire, on s'arrête là (pas de README inutile)
        if not generated:
            logger.info("[EXPORT] Aucune correction de pochette requise.")
            return
        
        # --- README récapitulatif ---
        readme_path = base.parent / f"{base.name}_MP3TAG_README.txt"
        readme_path.write_text(
            self._build_readme(xlsx_path, generated),
            encoding='utf-8',
        )
        logger.info(
            f"[EXPORT] Compagnons générés : "
            f"{len(generated)} correction(s), README inclus."
        )
    
    def _write_fix_pair(self, df: pd.DataFrame, base_path: Path,
                        suffix: str, mta_params: dict,
                        title: str, description: str) -> Optional[dict]:
        """Écrit un couple (.mta, .m3u) pour une correction donnée.
        
        Paramètres :
          df           : DataFrame des fichiers à traiter
          base_path    : préfixe commun (ex: /.../ZimaTAG_Audit_20260422)
          suffix       : suffixe distinctif (ex: 'FixCovers_NonJpg')
          mta_params   : dict {'1': fmt, '2': size, '3': qual}
          title        : titre lisible pour le README
          description  : description longue pour le README
        
        Retourne un dict avec les infos du couple généré (pour le README),
        ou None si la génération a échoué.
        """
        mta_path = base_path.parent / f"{base_path.name}_{suffix}.mta"
        m3u_path = base_path.parent / f"{base_path.name}_{suffix}.m3u"
        
        # --- Fichier .mta (UTF-16 LE + BOM + CRLF) ---
        # mta_params falsy (None/{}) = correction "liste seule" : pas d'action mp3tag.
        if mta_params:
            mta_lines = ["[#0]", "T=17"]
            for key in ('1', '2', '3'):  # ordre stable
                if key in mta_params:
                    mta_lines.append(f"{key}={mta_params[key]}")
            mta_content = "\r\n".join(mta_lines) + "\r\n"
            with open(mta_path, 'wb') as f:
                f.write(b'\xff\xfe')  # BOM UTF-16 LE
                f.write(mta_content.encode('utf-16-le'))
        else:
            mta_path = None
        
        # --- Fichier .m3u avec traduction Linux → Windows ---
        path_col = 'file_path' if 'file_path' in df.columns \
            else ('filepath' if 'filepath' in df.columns else None)
        if path_col is None:
            logger.warning(
                f"[EXPORT] Pas de colonne file_path/filepath dans {suffix} : "
                f"M3U non généré"
            )
            return None
        
        n_written = 0
        with open(m3u_path, 'w', encoding='utf-8') as f:
            f.write("#EXTM3U\n")
            f.write(f"# Généré par ZimaTAG v{EXPORTER_VERSION} "
                    f"le {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")
            f.write(f"# {title} : {len(df):,} fichier(s)\n")
            if mta_path is not None:
                f.write(f"# Action mp3tag associée : {mta_path.name}\n")
            else:
                f.write("# Liste seule : aucune action mp3tag (inspection manuelle).\n")
            f.write("# Les chemins ont été traduits vers Windows pour mp3tag.\n")
            f.write("# Mapping appliqué :\n")
            for linux_prefix, win_prefix in self._get_path_mappings().items():
                f.write(f"#   {linux_prefix} -> {win_prefix}\n")
            f.write("#\n")
            for _, row in df.iterrows():
                raw_path = row.get(path_col)
                if pd.isna(raw_path) or not raw_path:
                    continue
                win_path = self._translate_path_for_windows(str(raw_path))
                artist = row.get('artist', '') if 'artist' in df.columns else ''
                title_val = row.get('title', '') if 'title' in df.columns else ''
                if pd.isna(artist):
                    artist = ''
                if pd.isna(title_val):
                    title_val = ''
                if artist or title_val:
                    f.write(f"#EXTINF:-1,{artist} - {title_val}\n")
                f.write(f"{win_path}\n")
                n_written += 1
        
        logger.info(
            f"[EXPORT] {suffix} : "
            f"{mta_path.name if mta_path else '(liste seule)'} + {m3u_path.name} "
            f"({n_written} fichiers)"
        )
        return {
            'suffix': suffix,
            'title': title,
            'description': description,
            'mta_path': mta_path,
            'm3u_path': m3u_path,
            'n_files': n_written,
            'mta_params': mta_params,
        }
    
    def _build_readme(self, xlsx_path: Path, generated: list) -> str:
        """Construit le contenu du README récapitulatif pour les corrections générées."""
        mappings = self._get_path_mappings()
        mappings_str = "\n".join(
            f"      {linux_prefix}  ->  {win_prefix}"
            for linux_prefix, win_prefix in mappings.items()
        ) or "      (aucun mapping configuré)"
        
        # Section par correction
        corrections_sections = []
        for i, gen in enumerate(generated, start=1):
            params = gen['mta_params']
            if params:
                fmt_label = {0: 'Original', 1: 'JPEG', 2: 'PNG'}.get(params.get('1', 0), '?')
                size = params.get('2', '?')
                qual = params.get('3', '?')
                corrections_sections.append(
                    f"  [{i}] {gen['title']}\n"
                    f"      {gen['description']}\n"
                    f"\n"
                    f"      Fichier action   : {gen['mta_path'].name}\n"
                    f"      Playlist         : {gen['m3u_path'].name}\n"
                    f"      Fichiers à traiter : {gen['n_files']:,}\n"
                    f"      Paramètres action :\n"
                    f"        Format de sortie : {fmt_label} (1={params.get('1')})\n"
                    f"        Taille maximum   : {size} px\n"
                    f"        Qualité          : {qual}/100\n"
                )
            else:
                corrections_sections.append(
                    f"  [{i}] {gen['title']}\n"
                    f"      {gen['description']}\n"
                    f"\n"
                    f"      Playlist         : {gen['m3u_path'].name}\n"
                    f"      Fichiers à traiter : {gen['n_files']:,}\n"
                    f"      (Liste seule : aucune action mp3tag, inspection manuelle.)\n"
                )
        corrections_text = "\n".join(corrections_sections)
        
        return f"""================================================================================
 ZIMATAG - CORRECTIONS DE POCHETTES VIA MP3TAG
================================================================================
Export généré le {datetime.now().strftime('%d/%m/%Y à %H:%M')}
ZimaTAG excel_export v{EXPORTER_VERSION}
Excel associé : {xlsx_path.name}

RÉSUMÉ
--------------------------------------------------------------------------------
{len(generated)} correction(s) disponible(s) :

{corrections_text}

TRADUCTION DES CHEMINS (Linux → Windows)
--------------------------------------------------------------------------------
ZimaTAG tourne sur Linux (ZimaBoard) mais mp3tag tourne sur Windows.
Les chemins des playlists M3U ont été automatiquement traduits :

{mappings_str}

Si tu obtiens des erreurs "fichier inaccessible" dans mp3tag, c'est que
tes lecteurs réseau Windows utilisent d'autres lettres.
Pour modifier : Sidebar ZimaTAG > Mapping Linux → Windows > Enregistrer.

PROCÉDURE GÉNÉRALE
--------------------------------------------------------------------------------
 1/ IMPORTER LES ACTIONS dans mp3tag (une seule fois par type) :
    - Copie les fichiers .mta dans le dossier d'actions mp3tag :
        Windows : %APPDATA%\\Mp3tag\\data\\actions\\
        macOS   : ~/Library/Application Support/Mp3tag/data/actions/
    - Redémarre mp3tag.
    - Les actions apparaissent dans le menu Actions > Action Groups
      (visible aussi via Alt+A).

 2/ POUR CHAQUE CORRECTION, APPLIQUER :
    - Dans mp3tag : File > Open playlist... > sélectionne le .m3u correspondant.
    - Ctrl+A (tout sélectionner).
    - Alt+A > coche le groupe d'actions correspondant > OK.
    - Confirme l'exécution.

  [ATTENTION] Les actions modifient les fichiers de façon IRRÉVERSIBLE.
              Fais une sauvegarde avant, ou teste d'abord sur quelques
              fichiers pour valider le résultat visuellement.

VÉRIFICATION APRÈS TRAITEMENT
--------------------------------------------------------------------------------
  - Relance un scan complet dans ZimaTAG (bouton 🔄 Reset puis scan).
  - Génère un nouvel export Excel.
  - Les onglets correspondants doivent afficher
    "✅ Aucune anomalie détectée".

DÉTAIL TECHNIQUE DU FORMAT .MTA
--------------------------------------------------------------------------------
Chaque fichier .mta contient une action au format interne mp3tag :
    T=17   → type "Adjust Cover"
    1=X    → format de sortie (0=Original, 1=JPEG, 2=PNG)
    2=X    → taille maximum en pixels
    3=X    → qualité (0-100, 100 = meilleure)

Format confirmé par Florian Heidenreich (auteur de mp3tag) sur le forum
officiel, sous réserve d'évolution de l'outil.

================================================================================
"""

    # ==================================================================
    # Formats
    # ==================================================================
    def _init_formats(self):
        """Initialise tous les formats avec la charte graphique ZimaTAG."""
        C = self.COLORS

        # === HEADERS ===
        self.formats['header_main'] = self.workbook.add_format({
            'bold': True, 'font_name': 'Segoe UI', 'font_size': 11,
            'font_color': C['white'], 'bg_color': C['primary'],
            'align': 'center', 'valign': 'vcenter', 'text_wrap': True, 'border': 0,
        })
        self.formats['header_secondary'] = self.workbook.add_format({
            'bold': True, 'font_name': 'Segoe UI', 'font_size': 10,
            'font_color': C['white'], 'bg_color': C['secondary'],
            'align': 'center', 'valign': 'vcenter', 'border': 0,
        })

        # === TITRES ===
        self.formats['title_main'] = self.workbook.add_format({
            'bold': True, 'font_name': 'Segoe UI', 'font_size': 24, 'font_color': C['primary'],
        })
        self.formats['title_section'] = self.workbook.add_format({
            'bold': True, 'font_name': 'Segoe UI', 'font_size': 14,
            'font_color': C['primary'], 'bottom': 2, 'bottom_color': C['primary'],
        })
        self.formats['subtitle'] = self.workbook.add_format({
            'bold': True, 'font_name': 'Segoe UI', 'font_size': 11, 'font_color': C['secondary'],
        })

        # === CELLULES DONNÉES ===
        self.formats['cell'] = self.workbook.add_format({
            'font_name': 'Calibri', 'font_size': 10, 'valign': 'vcenter', 'border': 0,
        })
        self.formats['cell_zebra'] = self.workbook.add_format({
            'font_name': 'Calibri', 'font_size': 10, 'bg_color': C['zebra'],
            'valign': 'vcenter', 'border': 0,
        })

        # === CELLULES MONOSPACE (hash MD5/SHA, paths longs) ===
        # Police monospace : indispensable pour lire un hash hexadécimal.
        self.formats['cell_mono'] = self.workbook.add_format({
            'font_name': 'Consolas', 'font_size': 10, 'valign': 'vcenter', 'border': 0,
        })
        self.formats['cell_mono_zebra'] = self.workbook.add_format({
            'font_name': 'Consolas', 'font_size': 10, 'bg_color': C['zebra'],
            'valign': 'vcenter', 'border': 0,
        })

        # === NOMBRES ===
        self.formats['number'] = self.workbook.add_format({
            'font_name': 'Calibri', 'font_size': 10, 'num_format': '#,##0',
            'align': 'right', 'border': 0,
        })
        self.formats['number_zebra'] = self.workbook.add_format({
            'font_name': 'Calibri', 'font_size': 10, 'num_format': '#,##0',
            'align': 'right', 'bg_color': C['zebra'], 'border': 0,
        })
        self.formats['decimal'] = self.workbook.add_format({
            'font_name': 'Calibri', 'font_size': 10, 'num_format': '#,##0.00',
            'align': 'right', 'border': 0,
        })
        self.formats['percent'] = self.workbook.add_format({
            'font_name': 'Calibri', 'font_size': 10, 'num_format': '0.0%',
            'align': 'right', 'border': 0,
        })

        # === KPI CARDS ===
        self.formats['kpi_value'] = self.workbook.add_format({
            'bold': True, 'font_name': 'Segoe UI', 'font_size': 28,
            'font_color': C['primary'], 'align': 'center', 'valign': 'vcenter',
        })
        self.formats['kpi_label'] = self.workbook.add_format({
            'font_name': 'Segoe UI', 'font_size': 10, 'font_color': C['dark'],
            'align': 'center', 'valign': 'top',
        })
        self.formats['kpi_unit'] = self.workbook.add_format({
            'font_name': 'Segoe UI', 'font_size': 9, 'font_color': '#666666',
            'align': 'center', 'valign': 'vcenter',
        })

        # === HEALTH SCORE ===
        self.formats['health_good'] = self.workbook.add_format({
            'bold': True, 'font_name': 'Segoe UI', 'font_size': 36,
            'font_color': C['white'], 'bg_color': C['success'],
            'align': 'center', 'valign': 'vcenter',
        })
        self.formats['health_warning'] = self.workbook.add_format({
            'bold': True, 'font_name': 'Segoe UI', 'font_size': 36,
            'font_color': C['dark'], 'bg_color': C['warning'],
            'align': 'center', 'valign': 'vcenter',
        })
        self.formats['health_bad'] = self.workbook.add_format({
            'bold': True, 'font_name': 'Segoe UI', 'font_size': 36,
            'font_color': C['white'], 'bg_color': C['danger'],
            'align': 'center', 'valign': 'vcenter',
        })

        # === STATUTS ===
        self.formats['status_success'] = self.workbook.add_format({
            'font_name': 'Calibri', 'font_size': 10, 'font_color': '#155724',
            'bg_color': '#D4EDDA', 'align': 'center', 'border': 0,
        })
        self.formats['status_warning'] = self.workbook.add_format({
            'font_name': 'Calibri', 'font_size': 10, 'font_color': '#856404',
            'bg_color': '#FFF3CD', 'align': 'center', 'border': 0,
        })
        self.formats['status_error'] = self.workbook.add_format({
            'font_name': 'Calibri', 'font_size': 10, 'font_color': '#721C24',
            'bg_color': '#F8D7DA', 'align': 'center', 'border': 0,
        })

        # === LIENS ===
        self.formats['link'] = self.workbook.add_format({
            'font_name': 'Segoe UI', 'font_size': 10,
            'font_color': '#0066CC', 'underline': True,
        })
        # Bouton "Retour Cockpit" : plus visible
        self.formats['back_button'] = self.workbook.add_format({
            'bold': True, 'font_name': 'Segoe UI', 'font_size': 11,
            'font_color': C['white'], 'bg_color': C['primary'],
            'align': 'center', 'valign': 'vcenter', 'border': 0,
            'underline': 1,
        })

        # === NOTES ===
        self.formats['note'] = self.workbook.add_format({
            'font_name': 'Calibri', 'font_size': 9, 'font_color': '#666666', 'italic': True,
        })

    # ==================================================================
    # Cockpit
    # ==================================================================
    def _cockpit_logo_imgdata(self, height=44):
        """Logo Cockpit : (BytesIO PNG redimensionne, w, h) ou None si absent (lit data/icone, override ZIMA_ICONE_DIR)."""
        try:
            from pathlib import Path
            from io import BytesIO
            from PIL import Image
            base = Path(os.environ.get("ZIMA_ICONE_DIR", "/app_data/icone"))
            f = base / "Icone zimacompare.png"
            if not f.is_file():
                return None
            try:
                _resample = Image.Resampling.LANCZOS
            except AttributeError:
                _resample = Image.LANCZOS
            im = Image.open(f)
            im.load()
            if im.mode not in ("RGB", "RGBA"):
                im = im.convert("RGBA")
            w, h = im.size
            if h > height:
                nw = max(1, int(round(w * height / float(h))))
                im = im.resize((nw, height), _resample)
                w, h = nw, height
            bio = BytesIO()
            im.save(bio, format="PNG", optimize=True)
            bio.seek(0)
            return bio, w, h
        except Exception:
            return None

    def _create_cockpit(self):
        """Crée le Cockpit (Dashboard principal) avec KPI, charts et nav.

        La feuille Cockpit a déjà été créée en amont dans `export()`
        (self._cockpit_ws) pour être la première feuille visible. Cette
        méthode ne fait que la peupler.
        """
        ws = self._cockpit_ws
        if ws is None:
            # Fallback si l'initialisation a échoué
            ws = self.workbook.add_worksheet(self.COCKPIT_SHEET)
            ws.set_tab_color(self.COLORS['primary'])
        ws.hide_gridlines(2)

        # Colonnes
        ws.set_column('A:A', 7)  # V10-3 : marge gauche elargie pour le logo
        ws.set_column('B:G', 18)
        ws.set_column('H:H', 3)
        ws.set_column('I:N', 18)

        df = self.df_main

        # === HEADER ===
        ws.merge_range('B1:G1', '🎯 ZimaTAG Audit Report', self.formats['title_main'])
        ws.set_row(0, 36)
        # V10-3 : logo en-tete Cockpit (incruste depuis data/icone, skip si absent)
        _logo = self._cockpit_logo_imgdata()
        if _logo is not None:
            _bio, _lw, _lh = _logo
            ws.insert_image('A1', 'zimalogo.png', {
                'image_data': _bio,
                'x_offset': 3, 'y_offset': 3,
                'object_position': 1,
            })
        # Ligne 2 : date de génération + version du module (auto-vérification
        # du déploiement — si la version ci-dessous ne correspond pas à ce que
        # tu attends, c'est que l'ancienne version de excel_export.py est
        # toujours chargée par Streamlit, pense à redémarrer l'app).
        ws.write(
            'B2',
            f'Généré le {datetime.now().strftime("%d/%m/%Y à %H:%M")}  '
            f'•  excel_export v{EXPORTER_VERSION} ({EXPORTER_BUILD_DATE})',
            self.formats['note'],
        )
        ws.write('B3', f'Collection : {len(df):,} fichiers analysés',
                 self.formats['subtitle'])

        # === HEALTH SCORE (I2:N6) ===
        score, score_details = self._compute_health_score()
        ws.write('I2', 'SCORE DE SANTÉ', self.formats['title_section'])
        if score >= 80:
            score_fmt = self.formats['health_good']
        elif score >= 50:
            score_fmt = self.formats['health_warning']
        else:
            score_fmt = self.formats['health_bad']
        ws.merge_range('I3:K6', f'{score} / 100', score_fmt)
        ws.set_row(2, 28); ws.set_row(3, 28); ws.set_row(4, 28); ws.set_row(5, 28)

        # Détails du score (top 3 pénalités)
        ws.write('L3', 'Principaux impacts :', self.formats['subtitle'])
        for i, (label, penalty) in enumerate(score_details[:3]):
            ws.write(3 + i, 11, f'• {label}', self.formats['note'])
            ws.write(3 + i, 13, f'−{penalty:.1f} pts', self.formats['note'])

        row = 7

        # === KPI CARDS ROW 1 ===
        ws.write(row, 1, 'STATISTIQUES GLOBALES', self.formats['title_section'])
        row += 2

        kpis_row1 = [
            (len(df), 'Fichiers', 'total'),
            (df['album'].nunique() if 'album' in df.columns else 0, 'Albums', 'uniques'),
            (df['artist'].nunique() if 'artist' in df.columns else 0, 'Artistes', 'uniques'),
            (df['albumartist'].nunique() if 'albumartist' in df.columns else 0,
             'Album Artists', 'uniques'),
            (df['genre'].nunique() if 'genre' in df.columns else 0, 'Genres', 'différents'),
        ]
        col = 1
        for value, label, unit in kpis_row1:
            ws.write(row, col, value, self.formats['kpi_value'])
            ws.write(row + 1, col, label, self.formats['kpi_label'])
            ws.write(row + 2, col, unit, self.formats['kpi_unit'])
            col += 1
        ws.set_row(row, 36)
        row += 5

        # === KPI CARDS ROW 2 ===
        size_gb = round(df['size_mb'].sum() / 1024, 2) if 'size_mb' in df.columns else 0
        duration_h = round(df['duration_seconds'].sum() / 3600, 1) \
            if 'duration_seconds' in df.columns else 0

        with_cover = 0
        if 'has_cover' in df.columns:
            with_cover = int((df['has_cover'] == 'Yes').sum())
        cover_pct = with_cover / len(df) * 100 if len(df) > 0 else 0

        errors = 0
        if 'error' in df.columns:
            error_mask = df['error'].notna() & (df['error'].astype(str).str.strip() != '')
            errors = int(error_mask.sum())

        kpis_row2 = [
            (size_gb, 'Taille Totale', 'GB'),
            (duration_h, 'Durée Totale', 'heures'),
            (f'{cover_pct:.1f}%', 'Avec Pochette', f'{with_cover:,} fichiers'),
            (errors, 'Erreurs', 'fichiers'),
        ]
        col = 1
        for value, label, unit in kpis_row2:
            ws.write(row, col, value, self.formats['kpi_value'])
            ws.write(row + 1, col, label, self.formats['kpi_label'])
            ws.write(row + 2, col, unit, self.formats['kpi_unit'])
            col += 1
        ws.set_row(row, 36)
        row += 5

        # === EXECUTIVE SUMMARY — TOP 5 DES PROBLÈMES À TRAITER ===
        ws.write(row, 1, '🚨 TOP 5 DES PROBLÈMES À TRAITER', self.formats['title_section'])
        row += 2
        top_issues = self._compute_top_issues(limit=5)
        if top_issues:
            # En-têtes tableau
            ws.write(row, 1, 'Priorité', self.formats['header_secondary'])
            ws.write(row, 2, 'Onglet', self.formats['header_secondary'])
            ws.write(row, 3, 'Anomalies', self.formats['header_secondary'])
            ws.write(row, 4, 'Groupe', self.formats['header_secondary'])
            ws.set_row(row, 22)
            row += 1
            for i, (sheet_name, count, group_name) in enumerate(top_issues, start=1):
                ws.write(row, 1, f'#{i}', self.formats['cell'])
                ws.write_url(row, 2, f"internal:'{sheet_name}'!A1",
                             self.formats['link'], sheet_name)
                ws.write_number(row, 3, count, self.formats['number'])
                ws.write(row, 4, group_name.capitalize(), self.formats['cell'])
                row += 1
        else:
            ws.write(row, 1, '✅ Aucune anomalie significative détectée.',
                     self.formats['status_success'])
            row += 1
        row += 2

        # === GRAPHIQUE CAMEMBERT - FORMATS ===
        ws.write(row, 1, 'RÉPARTITION PAR FORMAT', self.formats['title_section'])
        row += 2
        formats_n = self._render_pie_formats(ws, anchor_row=row)
        row += max(formats_n, 12) + 2

        # === PIE - FORMATS DE POCHETTES (JPG/PNG/...) ===
        # Seulement si on détecte une colonne qui renseigne le format
        # (voir snippet de scanner dans la doc pour enrichir le CSV).
        if self._detect_cover_format_column() is not None:
            ws.write(row, 1, 'FORMATS DES POCHETTES (JPG / PNG / …)',
                     self.formats['title_section'])
            row += 2
            cover_n = self._render_pie_cover_formats(ws, anchor_row=row)
            row += max(cover_n, 12) + 2

        # === GRAPHIQUE HISTOGRAMME - ANNÉES ===
        ws.write(row, 1, 'ÉVOLUTION PAR ANNÉE DE SORTIE', self.formats['title_section'])
        row += 2
        years_n = self._render_bar_years(ws, anchor_row=row)
        row += max(years_n, 12) + 2

        # === GRAPHIQUE ANNEAU - COMPLÉTION TAGS ===
        ws.write(row, 1, 'TAUX DE COMPLÉTION DES TAGS', self.formats['title_section'])
        row += 2
        comp_n = self._render_donut_completion(ws, anchor_row=row)
        row += max(comp_n, 12) + 2

        # === NAVIGATION RAPIDE (avec compteurs et icônes) ===
        ws.write(row, 1, 'NAVIGATION RAPIDE', self.formats['title_section'])
        row += 2

        # En-têtes du tableau de navigation
        ws.write(row, 1, 'Onglet', self.formats['header_secondary'])
        ws.write(row, 2, 'Groupe', self.formats['header_secondary'])
        ws.write(row, 3, 'Lignes', self.formats['header_secondary'])
        ws.write(row, 4, 'Statut', self.formats['header_secondary'])
        ws.set_row(row, 22)
        row += 1

        for group_name, sheets in self.SHEET_GROUPS.items():
            if group_name == 'cockpit':
                continue
            for sheet_name, data_key in sheets:
                count = self._get_row_count(data_key)
                if count == 0:
                    icon, icon_fmt = '✅ Clean', self.formats['status_success']
                elif count < 10:
                    icon, icon_fmt = '⚠️ À vérifier', self.formats['status_warning']
                else:
                    icon, icon_fmt = '❌ Important', self.formats['status_error']

                ws.write_url(row, 1, f"internal:'{sheet_name}'!A1",
                             self.formats['link'], sheet_name)
                ws.write(row, 2, group_name.capitalize(), self.formats['cell'])
                ws.write_number(row, 3, count, self.formats['number'])
                ws.write(row, 4, icon, icon_fmt)
                row += 1

        # Gel des 4 premières lignes (titre + sous-titres)
        ws.freeze_panes(4, 0)

    # ==================================================================
    # Feuille de données (onglets d'audit)
    # ==================================================================
    def _create_data_sheet(self, sheet_name: str, data_key: str, group: str):
        """Crée un onglet de données formaté professionnellement.

        Layout :
          - Ligne 0 : bouton "🏠 Retour Cockpit" + titre
          - Ligne 1 : en-têtes de colonnes
          - Ligne 2+ : données
          - Freeze panes sur (2, 0)
        """
        data = self.audit_results.get(data_key)
        safe_name = sheet_name[:31]

        # Cas 1 : aucune donnée / DataFrame vide -> feuille "clean"
        if data is None or not isinstance(data, pd.DataFrame) or data.empty:
            ws = self.workbook.add_worksheet(safe_name)
            ws.hide_gridlines(2)
            self._write_back_button(ws, sheet_name)
            ws.merge_range('B3:F3', '✅ Aucune anomalie détectée',
                           self.formats['status_success'])
            ws.set_row(2, 24)
            return

        df = data  # pas de copy : on ne modifie pas le dataframe
        ws = self.workbook.add_worksheet(safe_name)

        # Couleur d'onglet
        group_colors = {
            'kpi':         '#2E86AB',
            'qualite':     '#17A2B8',
            'integrite':   '#DC3545',
            'metadonnees': '#FFC107',
            'doublons':    '#6F42C1',
            'casse':       '#E83E8C',
            'images':      '#20C997',
            'donnees':     '#6C757D',
        }
        ws.set_tab_color(group_colors.get(group, self.COLORS['secondary']))
        ws.hide_gridlines(2)

        # --- Ligne 0 : Bouton retour + titre ---
        self._write_back_button(ws, sheet_name)

        # --- Ligne 1 : Headers ---
        header_row = 1
        data_start_row = 2
        ws.set_row(header_row, 25)
        for col, header in enumerate(df.columns):
            ws.write(header_row, col, str(header), self.formats['header_main'])

        # --- Détection des types de colonnes ---
        # include='number' couvre int32/64, float32/64, Int64 nullable, etc.
        numeric_cols = set(df.select_dtypes(include='number').columns)
        hash_cols = {c for c in df.columns if str(c).lower() in self.HASH_COLUMNS}

        # --- Écriture des données ---
        # Boucle optimisée : on évite le getattr df.columns[col_idx] répété
        col_names = list(df.columns)
        values = df.values
        fmt_cell = self.formats['cell']
        fmt_cell_z = self.formats['cell_zebra']
        fmt_num = self.formats['number']
        fmt_num_z = self.formats['number_zebra']
        fmt_mono = self.formats['cell_mono']
        fmt_mono_z = self.formats['cell_mono_zebra']

        for row_idx, row_data in enumerate(values):
            is_zebra = (row_idx % 2 == 1)
            excel_row = row_idx + data_start_row
            for col_idx, value in enumerate(row_data):
                col_name = col_names[col_idx]

                # NaN -> cellule vide
                if value is None or (isinstance(value, float) and np.isnan(value)):
                    ws.write(excel_row, col_idx, '', fmt_cell_z if is_zebra else fmt_cell)
                    continue

                # Colonne de hash : toujours string monospace, même si c'est
                # détecté comme numeric par pandas (rare mais possible)
                if col_name in hash_cols:
                    ws.write_string(
                        excel_row, col_idx, str(value),
                        fmt_mono_z if is_zebra else fmt_mono,
                    )
                    continue

                # Colonne numérique : écriture numérique uniquement si la
                # valeur est convertible sans perte
                if col_name in numeric_cols:
                    try:
                        num_val = float(value)
                        if np.isnan(num_val) or np.isinf(num_val):
                            ws.write(excel_row, col_idx, '',
                                     fmt_cell_z if is_zebra else fmt_cell)
                        else:
                            ws.write_number(
                                excel_row, col_idx, num_val,
                                fmt_num_z if is_zebra else fmt_num,
                            )
                    except (TypeError, ValueError):
                        ws.write_string(
                            excel_row, col_idx, str(value),
                            fmt_cell_z if is_zebra else fmt_cell,
                        )
                else:
                    ws.write_string(
                        excel_row, col_idx, str(value),
                        fmt_cell_z if is_zebra else fmt_cell,
                    )

        # --- Auto-filtre sur les en-têtes ---
        last_row = data_start_row + len(df) - 1
        if len(df) > 0:
            ws.autofilter(header_row, 0, last_row, len(df.columns) - 1)

        # --- Freeze panes ---
        ws.freeze_panes(data_start_row, 0)

        # --- Largeurs de colonnes (robuste aux NaN) ---
        for col_idx, col_name in enumerate(df.columns):
            width = self._compute_column_width(df, col_name)
            # Pour les colonnes de hash, on force une largeur suffisante
            if col_name in hash_cols:
                width = max(width, 36)  # 32 pour MD5 + marge
            ws.set_column(col_idx, col_idx, width)

        # --- Formatage conditionnel ---
        self._apply_conditional_formatting(ws, df, data_key,
                                           data_start_row=data_start_row)

    # ==================================================================
    # Helpers : bouton retour, largeur, etc.
    # ==================================================================
    def _write_back_button(self, ws, current_sheet_name: str):
        """Écrit un lien '🏠 Retour Cockpit' en ligne 0 de la feuille donnée."""
        # Colonnes A (0) à E (4) : bouton + titre
        ws.set_row(0, 24)

        # Lien retour dans la cellule A1 + titre fusionné
        # xlsxwriter n'accepte pas merge_range + write_url sur la même plage,
        # donc on écrit le lien en A1 et le titre en B1:E1.
        ws.write_url(
            0, 0,
            f"internal:'{self.COCKPIT_SHEET}'!A1",
            self.formats['back_button'],
            '🏠 Retour',
        )
        ws.set_column(0, 0, 12)

        # Titre de l'onglet à côté du bouton
        try:
            ws.merge_range(0, 1, 0, 4, current_sheet_name, self.formats['title_section'])
        except Exception:
            ws.write(0, 1, current_sheet_name, self.formats['title_section'])

    def _compute_column_width(self, df: pd.DataFrame, col_name: str) -> float:
        """Calcule la largeur optimale d'une colonne, robuste aux NaN."""
        header_len = len(str(col_name))
        if len(df) == 0:
            return min(max(header_len + 2, 10), 50)
        try:
            data_max = df[col_name].astype(str).str.len().max()
            if pd.isna(data_max):
                data_max = 0
            else:
                data_max = int(data_max)
        except Exception:
            data_max = 0
        return min(max(max(header_len, data_max) + 2, 10), 50)

    # ==================================================================
    # Health score & top issues
    # ==================================================================
    def _get_row_count(self, data_key: str) -> int:
        """Retourne le nombre de lignes d'un résultat d'audit."""
        data = self.audit_results.get(data_key)
        if isinstance(data, pd.DataFrame):
            return len(data)
        if data is not None and hasattr(data, '__len__'):
            try:
                return len(data)
            except TypeError:
                return 0
        return 0

    def _compute_health_score(self) -> Tuple[int, List[Tuple[str, float]]]:
        """Calcule un score 0-100 basé sur les audits.

        Le score démarre à 100 et chaque catégorie d'anomalie retire
        (count / total_fichiers) * weight * 100 points, borné par catégorie.
        Retourne (score, [(label, penalty), ...]) trié par pénalité décroissante.
        """
        from audit import report_model
        return report_model.compute_health_score(self.audit_results, self.df_main, self.SHEET_GROUPS, self.HEALTH_WEIGHTS)

    def _compute_top_issues(self, limit: int = 5) -> List[Tuple[str, int, str]]:
        """Retourne [(sheet_name, count, group_name), ...] des N plus gros problèmes.

        Exclut les feuilles informatives (music_tags, genre_stats) et les KPI.
        """
        from audit import report_model
        return report_model.compute_top_issues(self.audit_results, self.SHEET_GROUPS, limit)

    # ==================================================================
    # Graphiques du Cockpit (alimentés depuis _ChartData caché)
    # ==================================================================
    def _push_chart_data(self, label_col: str, rows: List[Tuple[Any, float]]
                         ) -> Tuple[str, str, int, int]:
        """Écrit une série dans _ChartData et retourne ses références.

        Retourne (col_letter_labels, col_letter_values, start_row_excel, end_row_excel)
        pour construire les formules 'A1:A5'.
        """
        ws = self._chart_data_ws
        start = self._chart_data_row
        # On écrit en colonnes A (0) et B (1) avec un en-tête
        ws.write(start, 0, label_col)
        ws.write(start, 1, 'value')
        for i, (lbl, val) in enumerate(rows, start=1):
            ws.write(start + i, 0, str(lbl))
            try:
                ws.write_number(start + i, 1, float(val))
            except (TypeError, ValueError):
                ws.write(start + i, 1, 0)
        # Laisse une ligne vide de séparation
        end = start + len(rows)
        self._chart_data_row = end + 2
        return ('A', 'B', start + 2, end + 1)  # Excel 1-indexed

    @staticmethod
    def _anchor_above(anchor_row: int) -> int:
        """[22] Retourne anchor_row - 1 borné à 0.
        
        Tous les rendus de graphiques (`_render_pie_formats`,
        `_render_pie_cover_formats`, `_render_bar_years`,
        `_render_donut_completion`) ont besoin d'écrire l'entête du
        tableau et le chart à `anchor_row - 1`. Si `anchor_row == 0`
        (cas pathologique mais possible si un appelant change la
        séquence du Cockpit), le calcul produit -1 qui est invalide
        pour `ws.write` / `ws.insert_chart`. Ce helper rend le code
        défensif sans impact fonctionnel sur les cas normaux
        (anchor_row >= 1 où la valeur retournée est inchangée).
        """
        return max(0, anchor_row - 1)

    def _render_pie_formats(self, ws, anchor_row: int) -> int:
        """Affiche le camembert des formats audio. Retourne le nb de lignes utilisées."""
        df = self.df_main
        if 'extension' not in df.columns or len(df) == 0:
            return 0

        formats_data = df['extension'].value_counts()
        if formats_data.empty:
            return 0

        # Tableau lisible dans le cockpit (à droite du chart)
        total = len(df)
        for i, (ext, count) in enumerate(formats_data.items()):
            ws.write(anchor_row + i, 1, str(ext).upper(), self.formats['cell'])
            ws.write_number(anchor_row + i, 2, int(count), self.formats['number'])
            ws.write(anchor_row + i, 3, f'{count / total * 100:.1f}%',
                     self.formats['cell'])

        # Données du chart dans la feuille cachée
        rows = [(str(ext).upper(), int(cnt)) for ext, cnt in formats_data.items()]
        col_c, col_v, r0, r1 = self._push_chart_data('format', rows)

        chart = self.workbook.add_chart({'type': 'pie'})
        chart.add_series({
            'name': 'Formats',
            'categories': f"='{self.CHART_DATA_SHEET}'!${col_c}${r0}:${col_c}${r1}",
            'values':     f"='{self.CHART_DATA_SHEET}'!${col_v}${r0}:${col_v}${r1}",
            'data_labels': {'percentage': True, 'font': {'size': 10}},
            'points': [
                {'fill': {'color': '#1F4E79'}},
                {'fill': {'color': '#2E86AB'}},
                {'fill': {'color': '#5BA4C6'}},
                {'fill': {'color': '#8CC4DE'}},
            ],
        })
        chart.set_title({'name': 'Répartition par Format',
                         'name_font': {'size': 12, 'color': '#1F4E79'}})
        chart.set_style(10)
        chart.set_size({'width': 400, 'height': 280})
        chart.set_legend({'position': 'right'})
        ws.insert_chart(self._anchor_above(anchor_row), 4, chart)

        return len(formats_data)

    def _detect_cover_format_column(self) -> Optional[str]:
        """Détecte automatiquement le nom de colonne indiquant le format des
        pochettes dans df_main.

        Candidats courants (selon ce que le scanner a produit) :
        - cover_format, cover_mime, cover_mime_type, cover_type, cover_ext,
          cover_image_format, artwork_format, art_format, picture_format

        Retourne le nom de la colonne trouvée ou None.
        """
        if self.df_main is None:
            return None
        candidates = [
            'cover_format', 'cover_mime', 'cover_mime_type', 'cover_type',
            'cover_ext', 'cover_image_format', 'artwork_format', 'art_format',
            'picture_format', 'cover_mimetype',
        ]
        for col in candidates:
            if col in self.df_main.columns:
                # Au moins une valeur non nulle pour valider
                non_empty = self.df_main[col].notna() & (
                    self.df_main[col].astype(str).str.strip() != ''
                )
                if non_empty.any():
                    return col
        return None

    @staticmethod
    def _normalize_cover_format(value: Any) -> str:
        """Normalise une valeur de format pochette en libellé court.

        Accepte : 'image/jpeg', 'JPEG', 'jpg', '.png', 'image/png', etc.
        Retourne : 'JPG', 'PNG', 'GIF', 'BMP', 'WEBP', 'AUTRE'.
        """
        if value is None:
            return 'AUCUN'
        s = str(value).strip().lower()
        if not s or s in ('nan', 'none', 'null'):
            return 'AUCUN'
        # Retirer le mimetype prefix
        if '/' in s:
            s = s.split('/', 1)[1]
        # Retirer le point initial
        s = s.lstrip('.')
        # Normalisations courantes
        mapping = {
            'jpeg': 'JPG', 'jpg': 'JPG', 'pjpeg': 'JPG',
            'png': 'PNG', 'apng': 'PNG',
            'gif': 'GIF',
            'bmp': 'BMP',
            'webp': 'WEBP',
            'tiff': 'TIFF', 'tif': 'TIFF',
        }
        return mapping.get(s, s.upper() or 'AUTRE')

    def _render_pie_cover_formats(self, ws, anchor_row: int) -> int:
        """Affiche le camembert JPG/PNG/… des pochettes. Retourne nb lignes."""
        col = self._detect_cover_format_column()
        if col is None:
            return 0
        df = self.df_main
        if len(df) == 0:
            return 0

        # Normalisation des valeurs
        norm = df[col].apply(self._normalize_cover_format)
        counts = norm.value_counts()
        # On exclut les AUCUN pour le chart mais on les mentionne en note
        no_cover_n = int(counts.get('AUCUN', 0))
        if 'AUCUN' in counts.index:
            counts = counts.drop('AUCUN')
        if counts.empty:
            # Aucune pochette détectée
            ws.write(anchor_row, 1, '⚠️ Aucune pochette détectée dans les métadonnées',
                     self.formats['status_warning'])
            return 2

        # Tableau visible
        total_with_cover = int(counts.sum())
        for i, (fmt, cnt) in enumerate(counts.items()):
            ws.write(anchor_row + i, 1, fmt, self.formats['cell'])
            ws.write_number(anchor_row + i, 2, int(cnt), self.formats['number'])
            pct = cnt / total_with_cover * 100 if total_with_cover > 0 else 0
            ws.write(anchor_row + i, 3, f'{pct:.1f}%', self.formats['cell'])

        if no_cover_n > 0:
            ws.write(anchor_row + len(counts), 1,
                     f'ℹ️ {no_cover_n:,} fichier(s) sans pochette (exclus du graphe)',
                     self.formats['note'])

        # Données chart dans feuille cachée
        rows = [(str(f), int(c)) for f, c in counts.items()]
        col_c, col_v, r0, r1 = self._push_chart_data('cover_format', rows)

        chart = self.workbook.add_chart({'type': 'pie'})
        # Palette : JPG en bleu (dominant), PNG en orange, autres en gris
        color_map = {
            'JPG':  '#2E86AB', 'PNG':  '#FFC107', 'WEBP': '#28A745',
            'GIF':  '#6F42C1', 'BMP':  '#E83E8C', 'TIFF': '#17A2B8',
        }
        points = [{'fill': {'color': color_map.get(str(f), '#6C757D')}}
                  for f, _ in counts.items()]

        chart.add_series({
            'name': 'Formats pochettes',
            'categories': f"='{self.CHART_DATA_SHEET}'!${col_c}${r0}:${col_c}${r1}",
            'values':     f"='{self.CHART_DATA_SHEET}'!${col_v}${r0}:${col_v}${r1}",
            'data_labels': {'category': True, 'percentage': True,
                            'font': {'size': 10}},
            'points': points,
        })
        chart.set_title({'name': 'Formats de pochettes',
                         'name_font': {'size': 12,
                                       'color': self.COLORS['primary']}})
        chart.set_style(10)
        chart.set_size({'width': 400, 'height': 280})
        chart.set_legend({'position': 'right'})
        ws.insert_chart(self._anchor_above(anchor_row), 4, chart)

        return len(counts) + 2

    def _render_bar_years(self, ws, anchor_row: int) -> int:
        """Affiche l'histogramme par année. Retourne le nb de lignes utilisées."""
        df = self.df_main
        if 'year' not in df.columns:
            return 0

        year_mask = df['year'].notna() & (df['year'].astype(str).str.strip() != '')
        df_years = df[year_mask].copy()
        if df_years.empty:
            return 0

        df_years['year'] = df_years['year'].astype(str).str[:4]
        df_years = df_years[df_years['year'].str.isdigit()]
        # Filtrage années plausibles (1900-2099)
        try:
            y_int = df_years['year'].astype(int)
            df_years = df_years[(y_int >= 1900) & (y_int <= 2099)]
        except Exception:
            pass

        if df_years.empty:
            return 0

        years_data = df_years['year'].value_counts().sort_index()
        if len(years_data) > 25:
            years_data = years_data.tail(25)

        # Tableau visible
        for i, (year, count) in enumerate(years_data.items()):
            ws.write(anchor_row + i, 1, year, self.formats['cell'])
            ws.write_number(anchor_row + i, 2, int(count), self.formats['number'])

        # Données chart dans feuille cachée
        rows = [(str(y), int(c)) for y, c in years_data.items()]
        col_c, col_v, r0, r1 = self._push_chart_data('year', rows)

        chart = self.workbook.add_chart({'type': 'column'})
        chart.add_series({
            'name': 'Albums',
            'categories': f"='{self.CHART_DATA_SHEET}'!${col_c}${r0}:${col_c}${r1}",
            'values':     f"='{self.CHART_DATA_SHEET}'!${col_v}${r0}:${col_v}${r1}",
            'fill': {'color': '#2E86AB'},
            'gap': 50,
        })
        chart.set_title({'name': 'Fichiers par Année',
                         'name_font': {'size': 12, 'color': '#1F4E79'}})
        chart.set_style(10)
        chart.set_size({'width': 500, 'height': 280})
        chart.set_legend({'none': True})
        chart.set_x_axis({'name': 'Année', 'label_position': 'low'})
        chart.set_y_axis({'name': 'Fichiers'})
        ws.insert_chart(self._anchor_above(anchor_row), 4, chart)

        return len(years_data)

    def _render_donut_completion(self, ws, anchor_row: int) -> int:
        """Affiche le taux de complétion des tags sous forme de bar chart horizontal.

        Le nom de la méthode est conservé (_render_donut_completion) pour la
        compatibilité d'appel, mais on utilise désormais un bar chart car :
          - un doughnut avec 6 segments quasi-égaux (cas fréquent quand les
            tags sont bien remplis) est illisible ;
          - un bar chart horizontal permet de lire immédiatement le pourcentage
            de chaque tag, même si tous sont proches de 100%.

        Les barres sont colorées selon le taux :
          - ≥ 90 % : vert (success)
          - 50–89 % : orange (warning)
          - < 50 %  : rouge (danger)

        Retourne le nombre de lignes utilisées (hauteur occupée par le chart).
        """
        df = self.df_main
        if len(df) == 0:
            return 0

        tags_check = ['title', 'artist', 'album', 'year', 'genre', 'albumartist']
        completion: List[Tuple[str, float, int]] = []
        for tag in tags_check:
            if tag in df.columns:
                mask = df[tag].notna() & (df[tag].astype(str).str.strip() != '')
                filled = int(mask.sum())
                pct = filled / len(df) * 100 if len(df) > 0 else 0
                completion.append((tag.title(), pct, filled))

        if not completion:
            return 0

        # Tableau visible à côté du chart
        # Colonnes : Tag | Taux | Remplis | Manquants
        # [22] _anchor_above borne à 0 pour éviter une écriture à un index
        # négatif si anchor_row=0 (cas pathologique, voir docstring du helper).
        header_row = self._anchor_above(anchor_row)
        headers = ['Tag', 'Taux', 'Remplis', 'Manquants']
        for i, h in enumerate(headers):
            ws.write(header_row, 1 + i, h, self.formats['header_secondary'])
        ws.set_row(header_row, 20)

        total = len(df)
        for i, (tag, pct, count) in enumerate(completion):
            ws.write(anchor_row + i, 1, tag, self.formats['cell'])
            # Écrit le pourcentage en NOMBRE pour qu'il soit utilisable par le chart
            ws.write_number(anchor_row + i, 2, round(pct, 1),
                            self.formats['decimal'])
            ws.write_number(anchor_row + i, 3, count, self.formats['number'])
            ws.write_number(anchor_row + i, 4, total - count, self.formats['number'])

        # Données du chart dans la feuille cachée (pourcentages, pas counts)
        rows = [(tag, round(pct, 1)) for tag, pct, _ in completion]
        col_c, col_v, r0, r1 = self._push_chart_data('tag_completion', rows)

        # Couleurs par point selon le taux
        points = []
        for _, pct, _ in completion:
            if pct >= 90:
                color = self.COLORS['success']
            elif pct >= 50:
                color = self.COLORS['warning']
            else:
                color = self.COLORS['danger']
            points.append({'fill': {'color': color}, 'border': {'color': color}})

        # Bar chart horizontal (type 'bar' = barres horizontales en xlsxwriter)
        chart = self.workbook.add_chart({'type': 'bar'})
        chart.add_series({
            'name': 'Taux de complétion (%)',
            'categories': f"='{self.CHART_DATA_SHEET}'!${col_c}${r0}:${col_c}${r1}",
            'values':     f"='{self.CHART_DATA_SHEET}'!${col_v}${r0}:${col_v}${r1}",
            'points':     points,
            'data_labels': {
                'value': True,
                'num_format': '0.0"%"',
                'font': {'size': 10, 'bold': True},
                'position': 'outside_end',
            },
            'gap': 60,
        })
        chart.set_title({'name': 'Taux de Complétion des Tags',
                         'name_font': {'size': 12, 'color': self.COLORS['primary']}})
        chart.set_style(11)
        chart.set_size({'width': 500, 'height': 300})
        chart.set_legend({'none': True})
        chart.set_x_axis({
            'name': 'Taux (%)',
            'min': 0, 'max': 100,
            'major_unit': 20,
            'num_format': '0"%"',
        })
        chart.set_y_axis({
            'name': '',
            'reverse': True,  # Premier tag en haut (ordre naturel de lecture)
        })
        ws.insert_chart(self._anchor_above(anchor_row), 6, chart)

        # Réserver la hauteur du chart (~ 15 lignes Excel)
        return max(len(completion) + 2, 15)

    # ==================================================================
    # Formatage conditionnel
    # ==================================================================
    def _apply_conditional_formatting(self, ws, df: pd.DataFrame, data_key: str,
                                      data_start_row: int = 2):
        """Applique le formatage conditionnel intelligent.

        data_start_row = indice Python 0-based de la première ligne de données.
        En Excel 1-based : cell_range commence à data_start_row + 1.
        """
        if len(df) == 0:
            return

        num_rows = len(df)
        C = self.COLORS
        excel_start = data_start_row + 1  # 1-based
        excel_end = data_start_row + num_rows  # inclusif

        numeric_columns = list(df.select_dtypes(include='number').columns)
        hash_cols = {c for c in df.columns if str(c).lower() in self.HASH_COLUMNS}

        # === BARRES DE DONNÉES pour colonnes numériques (hors hash) ===
        for col_name in numeric_columns:
            if col_name in hash_cols:
                continue
            col_idx = df.columns.get_loc(col_name)
            col_letter = self._col_letter(col_idx)
            cell_range = f'{col_letter}{excel_start}:{col_letter}{excel_end}'

            lname = str(col_name).lower()
            if any(x in lname for x in ['erreur', 'manquant', 'vide', 'anomal']):
                bar_color = C['danger']
            elif 'bitrate' in lname:
                bar_color = C['secondary']
            else:
                bar_color = C['success']

            ws.conditional_format(cell_range, {
                'type': 'data_bar',
                'bar_color': bar_color,
                'bar_solid': True,
                'bar_negative_color': C['danger'],
            })

        # === ÉCHELLE DE COULEURS pour bitrate ===
        for col_name in [c for c in df.columns if 'bitrate' in str(c).lower()]:
            col_idx = df.columns.get_loc(col_name)
            col_letter = self._col_letter(col_idx)
            cell_range = f'{col_letter}{excel_start}:{col_letter}{excel_end}'
            ws.conditional_format(cell_range, {
                'type': '3_color_scale',
                'min_color': '#F8D7DA', 'mid_color': '#FFF3CD', 'max_color': '#D4EDDA',
                'min_type': 'num', 'min_value': 64,
                'mid_type': 'num', 'mid_value': 192,
                'max_type': 'num', 'max_value': 320,
            })
            ws.conditional_format(cell_range, {
                'type': 'cell', 'criteria': '<', 'value': 128,
                'format': self.formats['status_error'],
            })

        # === TAUX / POURCENTAGES avec icônes ===
        pct_cols = [c for c in df.columns
                    if 'taux' in str(c).lower() or '%' in str(c)]
        for col_name in pct_cols:
            col_idx = df.columns.get_loc(col_name)
            col_letter = self._col_letter(col_idx)
            cell_range = f'{col_letter}{excel_start}:{col_letter}{excel_end}'
            ws.conditional_format(cell_range, {
                'type': 'icon_set',
                'icon_style': '3_traffic_lights',
                'icons': [
                    {'criteria': '>=', 'type': 'number', 'value': 80},
                    {'criteria': '>=', 'type': 'number', 'value': 50},
                    {'criteria': '<',  'type': 'number', 'value': 50},
                ],
            })

        # === ANNÉES INVALIDES ===
        if data_key == 'invalid_year_format' and 'Année saisie' in df.columns:
            col_idx = df.columns.get_loc('Année saisie')
            col_letter = self._col_letter(col_idx)
            cell_range = f'{col_letter}{excel_start}:{col_letter}{excel_end}'
            ws.conditional_format(cell_range, {
                'type': 'text', 'criteria': 'not containing', 'value': '20',
                'format': self.formats['status_warning'],
            })

        # === DOUBLONS MD5 : surligne les hash dupliqués dans toute la colonne ===
        # Note : avec notre format_mono, la valeur reste intégralement lisible.
        # On applique ce surlignage sur l'onglet dédié mais AUSSI sur
        # "Données complètes" (music_tags) pour repérer les doublons dans
        # la vue globale.
        if data_key in ('duplicates_md5', 'music_tags'):
            for hash_col in ('file_md5', 'md5'):
                if hash_col in df.columns:
                    col_idx = df.columns.get_loc(hash_col)
                    col_letter = self._col_letter(col_idx)
                    cell_range = f'{col_letter}{excel_start}:{col_letter}{excel_end}'
                    ws.conditional_format(cell_range, {
                        'type': 'duplicate',
                        'format': self.formats['status_warning'],
                    })
                    break

        # === ÉCHELLE TAILLE FICHIERS ===
        size_cols = [c for c in df.columns
                     if 'size' in str(c).lower() or 'taille' in str(c).lower()]
        for col_name in size_cols:
            col_idx = df.columns.get_loc(col_name)
            col_letter = self._col_letter(col_idx)
            cell_range = f'{col_letter}{excel_start}:{col_letter}{excel_end}'
            ws.conditional_format(cell_range, {
                'type': '3_color_scale',
                'min_color': '#D4EDDA', 'mid_color': '#FFF3CD', 'max_color': '#F8D7DA',
            })

    def _col_letter(self, col_idx: int) -> str:
        """Convertit un index (0-based) de colonne en lettre Excel (0=A, 26=AA)."""
        result = ""
        col_idx += 1  # Excel est 1-indexed
        while col_idx > 0:
            col_idx, remainder = divmod(col_idx - 1, 26)
            result = chr(65 + remainder) + result
        return result


def export_to_excel(output_path: Optional[Path] = None) -> Path:
    """Fonction helper pour export simple."""
    exporter = ExcelExporter()
    return exporter.export(output_path)
