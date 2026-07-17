"""
audit/audit_engine.py - ZimaTAG Audit Engine
Moteur d'analyse de cohérence des métadonnées

Corrections appliquées :
  [15] _audit_invalid_year_format : remplacement de la boucle iterrows()
       par une opération vectorisée pandas. Sur une collection de 50 000
       fichiers le gain mesuré est typiquement de plusieurs secondes à
       quelques millisecondes.
  [16] _audit_kpi : remplacement du try/except brutal autour du cast
       astype(float) sur la colonne bitrate par pd.to_numeric(...,
       errors='coerce') qui ignore proprement les valeurs non numériques
       (chaînes vides, "320 kbps", NaN, etc.) sans masquer d'autres
       erreurs potentielles.
  [26] Uniformisation des gardes "colonne présente" :
       - Toute fonction d'audit vérifie désormais en début de méthode la
         présence des colonnes dont elle dépend strictement.
       - En cas de colonne(s) manquante(s), retour d'un DataFrame vide
         (comportement neutre, pas de KeyError silencieux ni de plantage).
       - Cas spécifiques préservés : _audit_kpi a déjà une logique
         tolérante (chaque KPI est gardé individuellement) — non touchée.
       - Bonus : _audit_album_gaps protégé contre le crash bitrate vide
         (variante robuste de [8] non sélectionnée mais incluse via la
         vectorisation, bug latent qui plantait silencieusement l'audit).
"""
import pandas as pd
from pathlib import Path
from typing import Dict, List, Tuple, Optional
from collections import defaultdict
from core import logger, config, db

# [LOT v20-3] Colonnes de la table SQLite tracks, dans l'ordre du CSV (id exclu).
# Duplique la liste deja presente dans engine/scanner.py/fingerprint_sqlite.py --
# dette de consolidation assumee, notee pour le nettoyage du LOT v20-7.
SQLITE_COLUMNS = [
    'filepath', 'filename', 'extension', 'directory', 'parent_folder',
    'size_mb', 'modified_date', 'file_md5', 'title', 'artist', 'album', 'albumartist',
    'composer', 'genre', 'year', 'track', 'tracktotal', 'disc', 'disctotal',
    'encoder', 'duration', 'duration_seconds', 'bitrate', 'samplerate',
    'channels', 'bitdepth', 'codec', 'id3_version', 'has_cover',
    'cover_size', 'cover_format', 'cover_width', 'cover_height',
    'cover_md5', 'cover_valid', 'cover_error', 'cover_count', 'error'
]


class AuditEngine:
    """Moteur d'audit des métadonnées"""
    
    def __init__(self, data_source=None):
        """
        Initialise le moteur d'audit.
        
        Args:
            data_source: Peut être un Path vers un CSV, un DataFrame, ou None (utilise le CSV par défaut)
        """
        if isinstance(data_source, pd.DataFrame):
            self.csv_path = None
            self.df = data_source.copy()
            self._preprocess_dataframe()
        elif isinstance(data_source, (str, Path)):
            self.csv_path = Path(data_source)
            self.df = None
        else:
            self.csv_path = config.master_csv_path
            self.df = None
        
        self.results: Dict[str, pd.DataFrame] = {}
    
    # ------------------------------------------------------------------
    # Helpers internes (uniformisation [26])
    # ------------------------------------------------------------------
    def _has_cols(self, *cols: str) -> bool:
        """Retourne True si TOUTES les colonnes nommées existent dans self.df.
        
        Helper introduit pour [26] : permet à chaque fonction d'audit de
        commencer par un garde unique et lisible, plutôt que de répéter
        des `if 'col' in self.df.columns` éparpillés.
        """
        if self.df is None:
            return False
        return all(c in self.df.columns for c in cols)
    
    def _preprocess_dataframe(self):
        """Prétraitement du DataFrame: remplace NaN par chaînes vides"""
        if self.df is None:
            return
        
        text_cols = ['title', 'artist', 'album', 'albumartist', 'composer', 
                    'genre', 'year', 'track', 'tracktotal', 'disc', 'disctotal',
                    'encoder', 'codec', 'id3_version', 'has_cover', 'error',
                    'filepath', 'filename', 'extension', 'directory', 'parent_folder',
                    'duration', 'cover_md5', 'file_md5']
        for col in text_cols:
            if col in self.df.columns:
                self.df[col] = self.df[col].fillna('').astype(str)
    
    def load_data(self) -> bool:
        """Charge les données depuis SQLite (master_scan.db) -- LOT v20-3"""
        if self.csv_path is None:
            return self.df is not None

        try:
            conn = db.connect()
            cols = ",".join(SQLITE_COLUMNS)
            self.df = pd.read_sql(f"SELECT {cols} FROM tracks ORDER BY id", conn)
            conn.close()
            self._preprocess_dataframe()
            logger.info(f"Données chargées (SQLite): {len(self.df)} lignes")
            return True
        except Exception as e:
            logger.error(f"Erreur chargement SQLite: {e}")
            return False
    
    def run_all_audits(self) -> Dict[str, pd.DataFrame]:
        """Exécute tous les audits"""
        if self.df is None:
            if not self.load_data():
                return {}
        
        audits = [
            ('kpi_dashboard', self._audit_kpi),
            ('kpi_years', self._audit_kpi_years),
            ('kpi_genres', self._audit_kpi_genres),
            ('kpi_albumartists', self._audit_kpi_albumartists),
            ('quality_analysis', self._audit_quality),
            ('album_gaps', self._audit_album_gaps),
            ('album_gaps_detailed', self._audit_album_gaps_detailed),
            ('incomplete_albums', self._audit_incomplete_albums),
            ('missing_genre_albums', self._audit_missing_genre_albums),
            ('missing_year_albums', self._audit_missing_year_albums),
            ('invalid_year_format', self._audit_invalid_year_format),
            ('albumartist_consistency', self._audit_albumartist_consistency),
            ('album_name_consistency', self._audit_album_name_consistency),
            ('year_inconsistency', self._audit_year_inconsistency),
            ('genre_inconsistency', self._audit_genre_inconsistency),
            ('track_gaps', self._audit_track_gaps),
            ('duplicates_md5', self._audit_duplicates_md5),
            ('duplicates_artist_title', self._audit_duplicates_artist_title),
            ('bitrate_anomalies', self._audit_bitrate_anomalies),
            ('bitrate_mixed_album', self._audit_bitrate_mixed_album),
            ('mojibake', self._audit_mojibake),
            ('id3_version_inconsistency', self._audit_id3_version_inconsistency),
            ('albumartist_typo', self._audit_albumartist_typo),
            ('folder_artist_mismatch', self._audit_folder_artist_mismatch),
            ('missing_metadata', self._audit_missing_metadata),
            ('samplerate_inconsistency', self._audit_samplerate_inconsistency),
            ('albumartist_vs_artist', self._audit_albumartist_vs_artist),
            ('cover_size', self._audit_cover_size),
            ('cover_non_uniform', self._audit_cover_non_uniform),
            ('covers_invalid', self._audit_covers_invalid),
            ('covers_too_small', self._audit_covers_too_small),
            ('multiple_covers', self._audit_multiple_covers),
            ('duration_zero', self._audit_duration_zero),
            ('windows_path_issues', self._audit_windows_path_issues),
            ('codec_homogeneity', self._audit_codec_homogeneity),
            ('genre_stats', self._audit_genre_stats),
            ('case_inconsistency_artist', self._audit_case_inconsistency_artist),
            ('case_inconsistency_album', self._audit_case_inconsistency_album),
            ('case_inconsistency_genre', self._audit_case_inconsistency_genre),
            ('case_by_artist_album', self._audit_case_by_artist_album)
        ]
        
        for name, func in audits:
            try:
                self.results[name] = func()
                logger.info(f"Audit {name}: {len(self.results[name])} lignes")
            except Exception as e:
                logger.error(f"Erreur audit {name}: {e}")
                self.results[name] = pd.DataFrame()
        
        return self.results
    
    def _audit_kpi(self) -> pd.DataFrame:
        """Dashboard KPI général"""
        df = self.df
        
        # Calcul erreurs avec vérification défensive
        errors_count = 0
        if 'error' in df.columns:
            error_mask = df['error'].notna() & (df['error'].astype(str) != '')
            errors_count = error_mask.sum()
        
        # [16] Calcul bitrate moyen MP3 — utilisation de pd.to_numeric pour
        # ignorer proprement les valeurs non numériques (chaînes vides,
        # "320 kbps", NaN, etc.) sans masquer d'autres erreurs avec un
        # except trop large.
        bitrate_avg = 0
        if 'extension' in df.columns and 'bitrate' in df.columns:
            mp3_df = df[df['extension'] == 'mp3']
            if len(mp3_df) > 0:
                br_numeric = pd.to_numeric(mp3_df['bitrate'], errors='coerce')
                # mean() ignore les NaN par défaut. Si tous NaN, on garde 0.
                mean_val = br_numeric.mean()
                if pd.notna(mean_val):
                    bitrate_avg = round(float(mean_val), 0)
        
        kpis = {
            'Total fichiers': len(df),
            'MP3': (df['extension'] == 'mp3').sum() if 'extension' in df.columns else 0,
            'FLAC': (df['extension'] == 'flac').sum() if 'extension' in df.columns else 0,
            'M4A': (df['extension'] == 'm4a').sum() if 'extension' in df.columns else 0,
            'Albums uniques': df['album'].nunique() if 'album' in df.columns else 0,
            'Artistes uniques': df['artist'].nunique() if 'artist' in df.columns else 0,
            'Album Artists uniques': df['albumartist'].nunique() if 'albumartist' in df.columns else 0,
            'Genres uniques': df['genre'].nunique() if 'genre' in df.columns else 0,
            'Avec pochette': (df['has_cover'] == 'Yes').sum() if 'has_cover' in df.columns else 0,
            'Sans pochette': (df['has_cover'] == 'No').sum() if 'has_cover' in df.columns else 0,
            # [LOT v20-5-fix] coercion numerique avant sum -- size_mb/duration_seconds
            # sont TEXT en SQLite (schema tout-TEXT du v20-1), .sum() brut concatenait
            # en chaine puis /1024 ou /3600 levait un TypeError (audit silencieusement
            # remplace par un DataFrame vide par run_all_audits()).
            'Taille totale (GB)': round(pd.to_numeric(df['size_mb'], errors='coerce').sum() / 1024, 2) if 'size_mb' in df.columns else 0,
            'Durée totale (h)': round(pd.to_numeric(df['duration_seconds'], errors='coerce').sum() / 3600, 2) if 'duration_seconds' in df.columns else 0,
            'Bitrate moyen MP3': bitrate_avg,
            'Erreurs extraction': errors_count
        }
        return pd.DataFrame(list(kpis.items()), columns=['Métrique', 'Valeur'])
    
    def _audit_kpi_years(self) -> pd.DataFrame:
        """KPI par année"""
        # [26] Garde colonnes
        if not self._has_cols('year'):
            return pd.DataFrame()
        
        df = self.df[self.df['year'] != ''].copy()
        if df.empty:
            return pd.DataFrame()
        
        df['year'] = df['year'].astype(str).str[:4]
        
        agg_dict = {'filename': 'count'}
        if 'album' in df.columns:
            agg_dict['album'] = 'nunique'
        if 'disc' in df.columns:
            agg_dict['disc'] = lambda x: x[x != ''].nunique()
        if 'albumartist' in df.columns:
            agg_dict['albumartist'] = 'nunique'
        
        stats = df.groupby('year').agg(agg_dict).reset_index()
        # Renommage adaptatif selon les colonnes effectivement agrégées
        rename_map = {
            'year': 'Année', 'album': 'Albums', 'disc': 'Disques',
            'albumartist': 'Album Artists', 'filename': 'Fichiers',
        }
        stats = stats.rename(columns=rename_map)
        return stats.sort_values('Année', ascending=False)
    
    def _audit_kpi_genres(self) -> pd.DataFrame:
        """KPI par genre"""
        # [26] Garde colonnes
        if not self._has_cols('genre'):
            return pd.DataFrame()
        
        df = self.df[self.df['genre'] != ''].copy()
        if df.empty:
            return pd.DataFrame()
        
        agg_dict = {'filename': 'count'}
        if 'album' in df.columns:
            agg_dict['album'] = 'nunique'
        if 'disc' in df.columns:
            agg_dict['disc'] = lambda x: x[x != ''].nunique()
        if 'albumartist' in df.columns:
            agg_dict['albumartist'] = 'nunique'
        if 'duration_seconds' in df.columns:
            # [LOT v20-5-fix] coercion inline (meme motif que le lambda disc
            # au-dessus) -- size_mb/duration_seconds TEXT en SQLite, sum brut
            # levait un TypeError.
            agg_dict['duration_seconds'] = lambda x: pd.to_numeric(x, errors='coerce').sum()
        if 'size_mb' in df.columns:
            agg_dict['size_mb'] = lambda x: pd.to_numeric(x, errors='coerce').sum()

        stats = df.groupby('genre').agg(agg_dict).reset_index()
        rename_map = {
            'genre': 'Genre', 'album': 'Albums', 'disc': 'Disques',
            'albumartist': 'Album Artists', 'filename': 'Fichiers',
            'duration_seconds': 'Durée (s)', 'size_mb': 'Taille (MB)',
        }
        stats = stats.rename(columns=rename_map)
        if 'Durée (s)' in stats.columns:
            stats['Durée (h)'] = round(stats['Durée (s)'] / 3600, 2)
        if 'Taille (MB)' in stats.columns:
            stats['Taille (GB)'] = round(stats['Taille (MB)'] / 1024, 2)
        return stats.sort_values('Fichiers', ascending=False)
    
    def _audit_kpi_albumartists(self) -> pd.DataFrame:
        """KPI par Album Artist avec codecs"""
        # [26] Garde colonnes
        if not self._has_cols('albumartist'):
            return pd.DataFrame()
        
        df = self.df[self.df['albumartist'] != ''].copy()
        if df.empty:
            return pd.DataFrame()
        
        agg_dict = {'filename': 'count'}
        if 'album' in df.columns:
            agg_dict['album'] = 'nunique'
        if 'codec' in df.columns:
            agg_dict['codec'] = lambda x: ', '.join(sorted(x.dropna().astype(str).unique()))
        if 'duration_seconds' in df.columns:
            # [LOT v20-5-fix] coercion inline (meme motif que le lambda codec
            # au-dessus) -- size_mb/duration_seconds TEXT en SQLite, sum brut
            # levait un TypeError.
            agg_dict['duration_seconds'] = lambda x: pd.to_numeric(x, errors='coerce').sum()
        if 'size_mb' in df.columns:
            agg_dict['size_mb'] = lambda x: pd.to_numeric(x, errors='coerce').sum()

        stats = df.groupby('albumartist').agg(agg_dict).reset_index()
        rename_map = {
            'albumartist': 'Album Artist', 'album': 'Albums',
            'filename': 'Fichiers', 'codec': 'Codecs',
            'duration_seconds': 'Durée (s)', 'size_mb': 'Taille (MB)',
        }
        stats = stats.rename(columns=rename_map)
        if 'Durée (s)' in stats.columns:
            stats['Durée (h)'] = round(stats['Durée (s)'] / 3600, 2)
        sort_col = 'Albums' if 'Albums' in stats.columns else 'Fichiers'
        return stats.sort_values(sort_col, ascending=False)
    
    def _audit_quality(self) -> pd.DataFrame:
        """Analyse qualité"""
        df = self.df
        quality = []
        
        # T10 Lot E1 (spec §2.6) : retrait MP3<seuil ; ajout Sans album-artiste,
        # Sans n de piste, Albums noms distincts, Albums dossiers.
        # Lignes "Sans X" (champ vide), dans l'ordre figé du §2.6.
        for col, label in (
            ('title', 'Sans titre'),
            ('artist', 'Sans artiste'),
            ('album', 'Sans album'),
            ('albumartist', 'Sans album-artiste'),
            ('year', 'Sans année'),
            ('genre', 'Sans genre'),
            ('track', 'Sans n° de piste'),
        ):
            if col in df.columns:
                quality.append({
                    'Catégorie': label,
                    'Nombre': int((df[col].astype(str) == '').sum()),
                })
        
        # Albums (noms distincts) : nombre de noms d'album uniques.
        if 'album' in df.columns:
            quality.append({
                'Catégorie': 'Albums (noms distincts)',
                'Nombre': int(df['album'].nunique()),
            })
        # Albums (dossiers) : couples (parent_folder, album) distincts = albums physiques.
        if 'parent_folder' in df.columns and 'album' in df.columns:
            quality.append({
                'Catégorie': 'Albums (dossiers)',
                'Nombre': int(df.groupby(['parent_folder', 'album']).ngroups),
            })
        
        return pd.DataFrame(quality)
    
    def _audit_album_gaps(self) -> pd.DataFrame:
        """Écarts par album (résumé).
        
        [26] Robuste aux colonnes manquantes : si album est absent, retour
        DataFrame vide. Les autres colonnes sont gardées individuellement
        dans le dict d'agrégation.
        Bonus robustesse [16] : bitrate.std() utilise pd.to_numeric pour
        éviter le crash sur cellule vide qui faisait échouer l'audit.
        """
        if not self._has_cols('album', 'parent_folder'):
            return pd.DataFrame()
        
        df = self.df[self.df['album'] != ''].copy()
        if df.empty:
            return pd.DataFrame()
        
        agg_dict = {'filename': 'count'}
        if 'year' in df.columns:
            agg_dict['year'] = lambda x: x.nunique()
        if 'genre' in df.columns:
            agg_dict['genre'] = lambda x: x.nunique()
        if 'albumartist' in df.columns:
            agg_dict['albumartist'] = lambda x: x.nunique()
        # T10 Lot B: colonne 'Écart bitrate' retirée (calculée mais hors filtre, trompeuse ;
        # std redondant avec bitrate_mixed_album). Agrégation bitrate désactivée.
        # if 'bitrate' in df.columns:
        #     agg_dict['bitrate'] = lambda x: pd.to_numeric(x, errors='coerce').std()
        
        grouped = df.groupby(['parent_folder', 'album']).agg(agg_dict).reset_index()
        rename_map = {
            'parent_folder': 'Dossier', 'album': 'Album',
            'year': 'Années diff.', 'genre': 'Genres diff.',
            'albumartist': 'Album Artists diff.',  # T10 Lot B: 'bitrate':'Écart bitrate' retiré
            'filename': 'Fichiers',
        }
        grouped = grouped.rename(columns=rename_map)
        
        # Construit le filtre de manière adaptative
        filters = []
        if 'Années diff.' in grouped.columns:
            filters.append(grouped['Années diff.'] > 1)
        if 'Genres diff.' in grouped.columns:
            filters.append(grouped['Genres diff.'] > 1)
        if 'Album Artists diff.' in grouped.columns:
            filters.append(grouped['Album Artists diff.'] > 1)
        
        if not filters:
            return pd.DataFrame()
        
        combined = filters[0]
        for f in filters[1:]:
            combined = combined | f
        return grouped[combined]
    
    def _audit_album_gaps_detailed(self) -> pd.DataFrame:
        """Écarts détaillés par album"""
        # [26] Garde colonnes
        if not self._has_cols('album', 'parent_folder'):
            return pd.DataFrame()
        
        df = self.df[self.df['album'] != ''].copy()
        if df.empty:
            return pd.DataFrame()
        
        details = []
        for (folder, album), group in df.groupby(['parent_folder', 'album']):
            years = group['year'].unique().tolist() if 'year' in group.columns else []
            genres = group['genre'].unique().tolist() if 'genre' in group.columns else []
            if len(years) > 1 or len(genres) > 1:
                details.append({
                    'Dossier': folder,
                    'Album': album,
                    'Années': ', '.join(str(y) for y in years if y),
                    'Genres': ', '.join(str(g) for g in genres if g),
                    'Fichiers': len(group)
                })
        return pd.DataFrame(details)
    
    def _audit_incomplete_albums(self) -> pd.DataFrame:
        """Albums incomplets avec détail par disque"""
        # [26] Garde colonnes : on a besoin au minimum de album/track/parent_folder
        if not self._has_cols('album', 'track', 'parent_folder'):
            return pd.DataFrame()
        
        df = self.df[(self.df['album'] != '') & (self.df['track'] != '')].copy()
        if df.empty:
            return pd.DataFrame()
        
        df['track_num'] = pd.to_numeric(df['track'], errors='coerce')
        if 'disc' in df.columns:
            df['disc_num'] = pd.to_numeric(df['disc'], errors='coerce').fillna(1).astype(int)
        else:
            df['disc_num'] = 1
        if 'tracktotal' in df.columns:
            df['tracktotal_num'] = pd.to_numeric(df['tracktotal'], errors='coerce')
        else:
            df['tracktotal_num'] = pd.NA
        if 'disctotal' in df.columns:
            df['disctotal_num'] = pd.to_numeric(df['disctotal'], errors='coerce')
        else:
            df['disctotal_num'] = pd.NA
        
        incomplete = []
        for (folder, album), group in df.groupby(['parent_folder', 'album']):
            # Infos album (chacune gardée individuellement)
            artist = group['artist'].mode().iloc[0] if 'artist' in group.columns and not group['artist'].mode().empty else ''
            albumartist = group['albumartist'].mode().iloc[0] if 'albumartist' in group.columns and not group['albumartist'].mode().empty else artist
            year = group['year'].mode().iloc[0] if 'year' in group.columns and not group['year'].mode().empty else ''
            genre = group['genre'].mode().iloc[0] if 'genre' in group.columns and not group['genre'].mode().empty else ''
            codec = group['codec'].mode().iloc[0] if 'codec' in group.columns and not group['codec'].mode().empty else ''
            directory = group['directory'].iloc[0] if 'directory' in group.columns and len(group) > 0 else ''
            
            # Analyse par disque
            discs = group.groupby('disc_num')
            disc_total_expected = group['disctotal_num'].max()
            disc_total_actual = group['disc_num'].max()
            
            issues = []
            total_tracks = 0
            
            for disc_num, disc_group in discs:
                tracks = sorted(disc_group['track_num'].dropna().astype(int).tolist())
                track_total = disc_group['tracktotal_num'].max()
                total_tracks += len(tracks)
                
                if pd.notna(track_total) and track_total > 0:
                    expected = set(range(1, int(track_total) + 1))
                    actual = set(tracks)
                    missing = sorted(expected - actual)
                    
                    if missing:
                        # Formater les plages de pistes manquantes
                        ranges = self._format_track_ranges(missing)
                        issues.append(f"D{int(disc_num)}: {ranges}")
            
            # Vérifier écart nombre de disques
            if pd.notna(disc_total_expected) and disc_total_actual < disc_total_expected:
                missing_discs = list(range(int(disc_total_actual) + 1, int(disc_total_expected) + 1))
                issues.append(f"Disques manquants: {', '.join(map(str, missing_discs))}")
            
            if issues:
                integrity = "❌ Manque(" + ", ".join(issues) + ")"
                incomplete.append({
                    'Artiste': albumartist or artist,
                    'Album': album,
                    'Nb Pistes': total_tracks,
                    'Intégrité': integrity,
                    'Année': year,
                    'Genre': genre,
                    'Codec Principal': codec,
                    'Chemin': directory
                })
        
        return pd.DataFrame(incomplete)
    
    def _format_track_ranges(self, tracks: list) -> str:
        """Formate une liste de pistes en plages (ex: 1-3, 5, 8-10)"""
        if not tracks:
            return ""
        
        ranges = []
        start = tracks[0]
        end = tracks[0]
        
        for t in tracks[1:]:
            if t == end + 1:
                end = t
            else:
                if start == end:
                    ranges.append(str(start))
                else:
                    ranges.append(f"{start} à {end}")
                start = end = t
        
        if start == end:
            ranges.append(str(start))
        else:
            ranges.append(f"{start} à {end}")
        
        return ", ".join(ranges)
    
    def _audit_missing_genre_albums(self) -> pd.DataFrame:
        """Albums avec genre vide ou manquant"""
        # [26] Garde colonnes
        if not self._has_cols('album', 'genre', 'parent_folder'):
            return pd.DataFrame()
        
        album_mask = self.df['album'].astype(str) != ''
        df = self.df[album_mask].copy()
        if df.empty:
            return pd.DataFrame()
        
        albums_missing = []
        for (folder, album), group in df.groupby(['parent_folder', 'album']):
            # Compte fichiers sans genre
            genre_mask = group['genre'].isna() | (group['genre'].astype(str) == '')
            empty_count = int(genre_mask.sum())
            
            if empty_count > 0:
                artist = ''
                if 'albumartist' in group.columns and not group['albumartist'].mode().empty:
                    artist = group['albumartist'].mode().iloc[0]
                if not artist and 'artist' in group.columns and not group['artist'].mode().empty:
                    artist = group['artist'].mode().iloc[0]
                
                albums_missing.append({
                    'Artiste': artist,
                    'Album': album,
                    'Fichiers sans genre': empty_count,
                    'Total fichiers': len(group),
                    'Taux vide (%)': round(empty_count / len(group) * 100, 1),
                    'Chemin': folder
                })
        
        return pd.DataFrame(albums_missing).sort_values('Fichiers sans genre', ascending=False) if albums_missing else pd.DataFrame()
    
    def _audit_missing_year_albums(self) -> pd.DataFrame:
        """Albums avec année vide ou manquante"""
        # [26] Garde colonnes
        if not self._has_cols('album', 'year', 'parent_folder'):
            return pd.DataFrame()
        
        album_mask = self.df['album'].astype(str) != ''
        df = self.df[album_mask].copy()
        if df.empty:
            return pd.DataFrame()
        
        albums_missing = []
        for (folder, album), group in df.groupby(['parent_folder', 'album']):
            # Compte fichiers sans année
            year_mask = group['year'].isna() | (group['year'].astype(str) == '')
            empty_count = int(year_mask.sum())
            
            if empty_count > 0:
                artist = ''
                if 'albumartist' in group.columns and not group['albumartist'].mode().empty:
                    artist = group['albumartist'].mode().iloc[0]
                if not artist and 'artist' in group.columns and not group['artist'].mode().empty:
                    artist = group['artist'].mode().iloc[0]
                genre = group['genre'].mode().iloc[0] if 'genre' in group.columns and not group['genre'].mode().empty else ''
                
                albums_missing.append({
                    'Artiste': artist,
                    'Album': album,
                    'Genre': genre,
                    'Fichiers sans année': empty_count,
                    'Total fichiers': len(group),
                    'Taux vide (%)': round(empty_count / len(group) * 100, 1),
                    'Chemin': folder
                })
        
        return pd.DataFrame(albums_missing).sort_values('Fichiers sans année', ascending=False) if albums_missing else pd.DataFrame()
    
    def _audit_invalid_year_format(self) -> pd.DataFrame:
        """Fichiers avec année mal formatée (pas 4 chiffres ∈ [1900, 2100]).
        
        [15] Vectorisé : remplace l'ancienne boucle iterrows() par des
        opérations pandas vectorisées. Sur 50 000 lignes le gain est
        d'environ deux ordres de grandeur (de plusieurs secondes à
        quelques millisecondes).
        Comportement strictement identique : même définition de "valide"
        (longueur exactement 4, isdigit, valeur entre 1900 et 2100), même
        diagnostic textuel, mêmes colonnes de sortie.
        """
        # [26] Garde colonne
        if not self._has_cols('year'):
            return pd.DataFrame()
        
        df = self.df
        # On ne traite que les années non-vides (année vide = "manquante",
        # géré par _audit_missing_year_albums, comportement existant préservé).
        year_mask = df['year'].notna() & (df['year'].astype(str).str.strip() != '')
        sub = df[year_mask].copy()
        if sub.empty:
            return pd.DataFrame()
        
        # Normalise en string strippée pour les contrôles
        year_s = sub['year'].astype(str).str.strip()
        
        # Critères de validité (identiques à l'ancienne logique itérative)
        len_eq_4 = year_s.str.len() == 4
        all_digit = year_s.str.isdigit()
        # int conversion safe (NaN si pas convertible) — uniquement pour tester
        # la borne ; les NaN seront gérés par fillna(False).
        as_int = pd.to_numeric(year_s, errors='coerce')
        in_range = (as_int >= 1900) & (as_int <= 2100)
        
        is_valid = len_eq_4 & all_digit & in_range.fillna(False)
        invalid_mask = ~is_valid
        
        if not invalid_mask.any():
            return pd.DataFrame()
        
        invalid = sub[invalid_mask].copy()
        invalid_years = year_s[invalid_mask]
        invalid_as_int = as_int[invalid_mask]
        
        # Diagnostic vectorisé — équivalent strict de _diagnose_year_issue
        # mais sans appel par ligne. Ordre des tests identique :
        #   1. len < 4   -> "Trop court (N car.)"
        #   2. len > 4   -> "Trop long (N car.)"
        #   3. !isdigit  -> "Contient des non-chiffres"
        #   4. < 1900    -> "Année trop ancienne (Y)"
        #   5. > 2100    -> "Année future invalide (Y)"
        #   6. fallback  -> "Format invalide"
        lens = invalid_years.str.len()
        digits = invalid_years.str.isdigit()
        
        problem = pd.Series('Format invalide', index=invalid_years.index)
        
        # Cas 6 (fallback) déjà initialisé. On surcharge dans l'ordre inverse
        # de priorité pour que le premier match l'emporte.
        # Cas 4-5 (année hors bornes — implique len==4 et isdigit)
        too_old = invalid_as_int < 1900
        too_new = invalid_as_int > 2100
        problem = problem.mask(too_old, invalid_as_int.astype('Int64').astype(str).radd('Année trop ancienne (').add(')'))
        problem = problem.mask(too_new, invalid_as_int.astype('Int64').astype(str).radd('Année future invalide (').add(')'))
        # Cas 3 (non-chiffres) — peut être combiné avec longueur correcte
        problem = problem.mask(~digits, 'Contient des non-chiffres')
        # Cas 1-2 (longueur incorrecte) — priorité supérieure aux autres
        problem = problem.mask(lens < 4, lens.astype(str).radd('Trop court (').add(' car.)'))
        problem = problem.mask(lens > 4, lens.astype(str).radd('Trop long (').add(' car.)'))
        
        # Construction du résultat. On préserve les noms de colonnes ET leur
        # ordre d'origine.
        artist_series = invalid.get('albumartist')
        if artist_series is None or (artist_series.astype(str) == '').all():
            artist_series = invalid.get('artist', pd.Series('', index=invalid.index))
        else:
            # Fallback ligne par ligne : si albumartist vide, prendre artist
            artist_fallback = invalid.get('artist', pd.Series('', index=invalid.index))
            artist_empty = artist_series.astype(str) == ''
            artist_series = artist_series.where(~artist_empty, artist_fallback)
        
        result = pd.DataFrame({
            'Artiste': artist_series.astype(str).values,
            'Album': invalid.get('album', pd.Series('', index=invalid.index)).astype(str).values,
            'Titre': invalid.get('title', pd.Series('', index=invalid.index)).astype(str).values,
            'Année saisie': invalid_years.values,
            'Problème': problem.values,
            'Fichier': invalid.get('filename', pd.Series('', index=invalid.index)).astype(str).values,
            'Chemin': invalid.get('directory', pd.Series('', index=invalid.index)).astype(str).values,
        })
        return result
    
    def _diagnose_year_issue(self, year: str) -> str:
        """Diagnostique le problème de format d'année.
        
        Conservé pour compatibilité ascendante (au cas où du code externe
        l'utilise). N'est plus appelée par _audit_invalid_year_format qui
        utilise désormais une version vectorisée équivalente.
        """
        if not year:
            return "Vide"
        if len(year) < 4:
            return f"Trop court ({len(year)} car.)"
        if len(year) > 4:
            return f"Trop long ({len(year)} car.)"
        if not year.isdigit():
            return "Contient des non-chiffres"
        try:
            y = int(year)
            if y < 1900:
                return f"Année trop ancienne ({y})"
            if y > 2100:
                return f"Année future invalide ({y})"
        except Exception:
            return "Non numérique"
        return "Format invalide"
    
    def _audit_albumartist_consistency(self) -> pd.DataFrame:
        """Cohérence Album Artist"""
        # [26] Garde colonnes
        if not self._has_cols('album', 'albumartist', 'parent_folder'):
            return pd.DataFrame()
        
        df = self.df[self.df['album'] != ''].copy()
        issues = []
        for (folder, album), group in df.groupby(['parent_folder', 'album']):
            artists = group['albumartist'].unique()
            if len(artists) > 1:
                issues.append({
                    'Dossier': folder,
                    'Album': album,
                    'Album Artists': ', '.join(str(a) for a in artists if a),
                    'Fichiers': len(group)
                })
        return pd.DataFrame(issues)
    
    def _audit_album_name_consistency(self) -> pd.DataFrame:
        """Cohérence nom album dans dossier"""
        # [26] Garde colonnes
        if not self._has_cols('album', 'parent_folder'):
            return pd.DataFrame()
        
        df = self.df.copy()
        issues = []
        for folder, group in df.groupby('parent_folder'):
            albums = group['album'].unique()
            if len(albums) > 1:
                issues.append({
                    'Dossier': folder,
                    'Albums': ', '.join(str(a) for a in albums if a),
                    'Fichiers': len(group)
                })
        return pd.DataFrame(issues)
    
    def _audit_year_inconsistency(self) -> pd.DataFrame:
        """Incohérences année"""
        # [26] Garde colonnes
        if not self._has_cols('album', 'year', 'parent_folder'):
            return pd.DataFrame()
        
        df = self.df[(self.df['album'] != '') & (self.df['year'] != '')].copy()
        issues = []
        for (folder, album), group in df.groupby(['parent_folder', 'album']):
            years = group['year'].unique()
            if len(years) > 1:
                issues.append({
                    'Dossier': folder,
                    'Album': album,
                    'Années': ', '.join(str(y) for y in years),
                    'Fichiers': len(group)
                })
        return pd.DataFrame(issues)
    
    def _audit_genre_inconsistency(self) -> pd.DataFrame:
        """Incohérences genre"""
        # [26] Garde colonnes
        if not self._has_cols('album', 'genre', 'parent_folder'):
            return pd.DataFrame()
        
        df = self.df[(self.df['album'] != '') & (self.df['genre'] != '')].copy()
        issues = []
        for (folder, album), group in df.groupby(['parent_folder', 'album']):
            genres = group['genre'].unique()
            if len(genres) > 1:
                issues.append({
                    'Dossier': folder,
                    'Album': album,
                    'Genres': ', '.join(str(g) for g in genres),
                    'Fichiers': len(group)
                })
        return pd.DataFrame(issues)
    
    def _audit_track_gaps(self) -> pd.DataFrame:
        """Trous numérotation"""
        # [26] Garde colonnes
        if not self._has_cols('album', 'track', 'parent_folder'):
            return pd.DataFrame()
        
        df = self.df[(self.df['album'] != '') & (self.df['track'] != '')].copy()
        df['track_num'] = pd.to_numeric(df['track'], errors='coerce')
        
        gaps = []
        for (folder, album), group in df.groupby(['parent_folder', 'album']):
            tracks = sorted(group['track_num'].dropna().astype(int).tolist())
            if tracks:
                expected = set(range(1, max(tracks) + 1))
                actual = set(tracks)
                missing = sorted(expected - actual)
                if missing:
                    gaps.append({
                        'Dossier': folder,
                        'Album': album,
                        'Manquantes': ', '.join(str(t) for t in missing),
                        'Présentes': len(actual)
                    })
        return pd.DataFrame(gaps)
    
    def _audit_duplicates_md5(self) -> pd.DataFrame:
        """Doublons basés sur le MD5 du fichier ou signature artist|title|duration"""
        # Essaie d'abord avec file_md5
        if 'file_md5' in self.df.columns:
            df = self.df[self.df['file_md5'] != ''].copy()
            if not df.empty:
                dups = df[df.duplicated(subset=['file_md5'], keep=False)]
                if not dups.empty:
                    cols = ['filepath', 'file_md5', 'artist', 'title', 'album', 'size_mb']
                    available = [c for c in cols if c in dups.columns]
                    return dups[available].sort_values(
                        [c for c in ('file_md5', 'filepath') if c in available]
                    )
        
        # Fallback: signature artist|title|duration
        if not self._has_cols('artist', 'title'):
            return pd.DataFrame()
        df = self.df[(self.df['artist'] != '') & (self.df['title'] != '')].copy()
        if df.empty:
            return pd.DataFrame()
        
        duration_col = df['duration'].astype(str) if 'duration' in df.columns else ''
        df['signature'] = df['artist'].astype(str) + '|' + df['title'].astype(str) + '|' + duration_col
        dups = df[df.duplicated(subset=['signature'], keep=False)]
        
        if dups.empty:
            return pd.DataFrame()
        
        cols = ['filepath', 'artist', 'title', 'album', 'duration']
        available = [c for c in cols if c in dups.columns]
        return dups[available].sort_values(
            [c for c in ('artist', 'title', 'filepath') if c in available]
        )
    
    def _audit_duplicates_artist_title(self) -> pd.DataFrame:
        """Doublons Artist/Title"""
        # [26] Garde colonnes
        if not self._has_cols('artist', 'title'):
            return pd.DataFrame()
        
        df = self.df[(self.df['artist'] != '') & (self.df['title'] != '')].copy()
        if df.empty:
            return pd.DataFrame()
        
        dups = df[df.duplicated(subset=['artist', 'title'], keep=False)]
        if dups.empty:
            return pd.DataFrame()
        cols = ['filepath', 'artist', 'title', 'album']
        available = [c for c in cols if c in dups.columns]
        return dups[available].sort_values(
            [c for c in ('artist', 'title') if c in available]
        )
    
    def _audit_bitrate_anomalies(self) -> pd.DataFrame:
        """Anomalies bitrate"""
        # [26] Garde colonnes (déjà présente, conservée)
        if 'extension' not in self.df.columns or 'bitrate' not in self.df.columns:
            return pd.DataFrame()
        
        mp3_mask = self.df['extension'] == 'mp3'
        df = self.df[mp3_mask].copy()
        if df.empty:
            return pd.DataFrame()
        df['bitrate_num'] = pd.to_numeric(df['bitrate'], errors='coerce')
        
        low_bitrate_mask = df['bitrate_num'] < config.MIN_BITRATE_MP3
        anomalies = df[low_bitrate_mask.fillna(False)]
        
        cols = ['filepath', 'artist', 'title', 'bitrate', 'album']
        available_cols = [c for c in cols if c in anomalies.columns]
        return anomalies[available_cols] if len(anomalies) > 0 else pd.DataFrame()
    
    def _audit_missing_metadata(self) -> pd.DataFrame:
        """Métadonnées manquantes"""
        df = self.df.copy()
        
        # Créer masques individuels
        masks = []
        for col in ['title', 'artist', 'album', 'year']:
            if col in df.columns:
                masks.append(df[col].astype(str) == '')
        
        if not masks:
            return pd.DataFrame()
        
        # Combiner les masques avec OR
        combined_mask = masks[0]
        for mask in masks[1:]:
            combined_mask = combined_mask | mask
        
        missing = df[combined_mask]
        cols = ['filepath', 'title', 'artist', 'album', 'year']
        available_cols = [c for c in cols if c in missing.columns]
        return missing[available_cols] if len(missing) > 0 else pd.DataFrame()
    
    def _audit_samplerate_inconsistency(self) -> pd.DataFrame:
        """Incohérences samplerate"""
        # [26] Garde colonnes
        if not self._has_cols('album', 'samplerate', 'parent_folder', 'disc'):
            return pd.DataFrame()
        
        df = self.df[self.df['album'] != ''].copy()
        df['disc_num'] = pd.to_numeric(df['disc'], errors='coerce').fillna(1).astype(int)  # T10 Lot E2
        issues = []
        for (folder, album, _disc), group in df.groupby(['parent_folder', 'album', 'disc_num']):
            rates = group['samplerate'].unique()
            if len(rates) > 1:
                issues.append({
                    'Dossier': folder,
                    'Album': album,
                    'Samplerates': ', '.join(str(r) for r in rates if r),
                    'Fichiers': len(group)
                })
        return pd.DataFrame(issues)
    
    def _audit_bitrate_mixed_album(self) -> pd.DataFrame:
        """Bitrate mixte intra-album (MP3) : ecart max-min >= seuil (INFO).

        F17 : ne flague que les MP3 d'un meme (parent_folder, album) dont
        l'etendue de bitrate depasse config.BITRATE_MIXED_RANGE_KBPS. Le
        melange de formats est couvert par _audit_codec_homogeneity.
        """
        if not self._has_cols('album', 'bitrate', 'parent_folder', 'extension', 'disc'):
            return pd.DataFrame()

        df = self.df[(self.df['album'] != '') & (self.df['extension'] == 'mp3')].copy()
        if df.empty:
            return pd.DataFrame()
        df['bitrate_num'] = pd.to_numeric(df['bitrate'], errors='coerce')
        df['disc_num'] = pd.to_numeric(df['disc'], errors='coerce').fillna(1).astype(int)  # T10 Lot E2

        seuil = getattr(config, 'BITRATE_MIXED_GAP_KBPS', 50)
        issues = []
        for (folder, album, _disc), group in df.groupby(['parent_folder', 'album', 'disc_num']):
            br = group['bitrate_num'].dropna()
            if len(br) < 2:
                continue
            vals = sorted({int(v) for v in br})
            gaps = [b - a for a, b in zip(vals, vals[1:])]
            saut_max = max(gaps) if gaps else 0
            if saut_max >= seuil:
                spread = vals[-1] - vals[0]
                issues.append({
                    'Dossier': folder,
                    'Album': album,
                    'Bitrates': ', '.join(str(v) for v in vals),
                    'Saut max': saut_max,
                    'Écart': spread,
                    'Fichiers': len(group),
                })
        return pd.DataFrame(issues)

    def _audit_mojibake(self) -> pd.DataFrame:
        """Mojibake (double-encodage UTF-8/Latin-1) dans les champs tag.

        F17 : un caractere A-tilde / A-circonflexe / a-circonflexe suivi d'un
        autre caractere non-ASCII = signature du double-encodage. Les accents
        legitimes (ex. 'Ame' = A-circonflexe + lettre ASCII) sont exclus.
        """
        cols = [c for c in ('title', 'artist', 'album', 'albumartist', 'composer', 'genre')
                if c in self.df.columns]
        if not cols or 'filepath' not in self.df.columns:
            return pd.DataFrame()
        rx = '[\u00c2\u00c3][\u0080-\u00bf]|\u00e2\u20ac'
        rows = []
        for c in cols:
            mask = self.df[c].astype(str).str.contains(rx, regex=True, na=False)
            for _, r in self.df[mask].iterrows():
                rows.append({
                    'filepath': r['filepath'],
                    'Champ': c,
                    'Valeur': r[c],
                })
        return pd.DataFrame(rows)

    def _audit_id3_version_inconsistency(self) -> pd.DataFrame:
        """Versions ID3 melangees dans un meme album (MP3) (INFO).

        F17 : flague les dossiers dont les MP3 ne sont pas tous sur la meme
        version ID3 (ex. v2.3 et v2.4 melanges). Tidiness, pas un defaut de
        lecture. FLAC / M4A ignores (pas d'ID3v2).
        """
        if not self._has_cols('album', 'id3_version', 'parent_folder', 'extension'):
            return pd.DataFrame()
        df = self.df[self.df['extension'] == 'mp3'].copy()
        issues = []
        for folder, group in df.groupby('parent_folder'):
            vers = [v for v in group['id3_version'].unique() if v]
            if len(vers) > 1:
                albums = [a for a in group['album'].unique() if a]
                issues.append({
                    'Dossier': folder,
                    'Album': albums[0] if albums else '',
                    'Versions': ', '.join(sorted(vers)),
                    'Fichiers': len(group),
                })
        return pd.DataFrame(issues)

    def _audit_albumartist_typo(self) -> pd.DataFrame:
        """Quasi-doublon / faute de frappe dans albumartist (INFO).

        F21 : une valeur albumartist rare et tres proche (distance d'edition
        <= 2) d'une valeur >= 10x plus frequente est probablement une typo
        (ex. 'Various Aritsts' -> 'Various Artists'). Haute precision :
        len >= 6 + fort ecart de frequence pour eviter les faux positifs
        (ex. groupes voisins Genesis/Nemesis). INFO, pas un defaut de lecture.
        """
        if not self._has_cols('albumartist', 'parent_folder'):
            return pd.DataFrame()
        RATIO, MAXDIST, MINLEN = 10, 2, 6

        def _lev(a, b):
            m, n = len(a), len(b)
            if m < n:
                a, b, m, n = b, a, n, m
            prev = list(range(n + 1))
            for i in range(1, m + 1):
                cur = [i] + [0] * n
                for j in range(1, n + 1):
                    cur[j] = min(prev[j] + 1, cur[j - 1] + 1,
                                 prev[j - 1] + (a[i - 1] != b[j - 1]))
                prev = cur
            return prev[n]

        s = self.df['albumartist'].fillna('').map(lambda x: str(x).strip())
        counts = s[s != ''].value_counts()
        cd = counts.to_dict()
        vals = list(counts.index)
        suspects = {}
        for v in vals:
            if len(v) < MINLEN:
                continue
            cv = cd[v]
            for w in vals:
                if w == v or cd[w] < cv * RATIO:
                    continue
                d = _lev(v.lower(), w.lower())
                if 0 < d <= MAXDIST:
                    suspects[v] = (w, d)
                    break
        if not suspects:
            return pd.DataFrame()
        work = self.df.copy()
        work['_aa'] = work['albumartist'].fillna('').map(lambda x: str(x).strip())
        work = work[work['_aa'].isin(suspects)]
        issues = []
        for (folder, aa), group in work.groupby(['parent_folder', '_aa']):
            sugg, dist = suspects[aa]
            issues.append({
                'Dossier': folder,
                'AlbumArtist': aa,
                'Suggestion': sugg,
                'Distance': dist,
                'Fichiers': len(group),
            })
        return pd.DataFrame(issues)

    def _audit_folder_artist_mismatch(self) -> pd.DataFrame:
        """Nom de dossier <-> albumartist incoherent (INFO).

        F22 : le dossier suit '<artiste> - <album>'. Le segment artiste (avant
        le 1er ' - ') doit correspondre a l'albumartist des pistes. Test
        canonique : normalisation NFD strip-accents + minuscules + espaces.
        Normalises egaux mais bruts differents -> Type 'accent' (accent
        manquant/different dans le nom de dossier, ex. Mylene vs Mylene
        accentue). Normalises differents -> Type 'mismatch' (ex. dossier
        'Various Artists - X' mais tag 'Moby'). Ignore : dossiers sans ' - ',
        multi-albumartist, ou albumartist multi '/'. INFO, pas un defaut.
        """
        if not self._has_cols('albumartist', 'parent_folder'):
            return pd.DataFrame()
        import unicodedata

        def _norm(s):
            s = unicodedata.normalize('NFD', s or '')
            s = ''.join(c for c in s if unicodedata.category(c) != 'Mn')
            return ' '.join(s.lower().split())

        work = self.df.copy()
        work['_aa'] = work['albumartist'].fillna('').map(lambda x: str(x).strip())
        issues = []
        for folder, grp in work.groupby('parent_folder'):
            if ' - ' not in folder:
                continue
            aas = sorted(set(a for a in grp['_aa'] if a))
            if len(aas) != 1 or '/' in aas[0]:
                continue
            aa = aas[0]
            fa = folder.split(' - ', 1)[0].strip()
            if _norm(fa) == _norm(aa):
                if fa == aa:
                    continue
                typ = 'accent'
            else:
                typ = 'mismatch'
            issues.append({
                'Dossier': folder,
                'ArtisteDossier': fa,
                'AlbumArtist': aa,
                'Type': typ,
                'Fichiers': len(grp),
            })
        return pd.DataFrame(issues)

    def _audit_albumartist_vs_artist(self) -> pd.DataFrame:
        """Album Artist ≠ Artist"""
        # [26] Garde colonnes
        if not self._has_cols('albumartist', 'artist'):
            return pd.DataFrame()
        
        df = self.df[
            (self.df['albumartist'] != '') & 
            (self.df['artist'] != '') &
            (self.df['albumartist'] != self.df['artist'])
        ].copy()
        cols = ['filepath', 'artist', 'albumartist', 'album']
        available = [c for c in cols if c in df.columns]
        return df[available]
    
    def _audit_cover_size(self) -> pd.DataFrame:
        """Taille pochettes"""
        # [26] Garde colonnes
        if not self._has_cols('has_cover', 'cover_size'):
            return pd.DataFrame()
        
        df = self.df[self.df['has_cover'] == 'Yes'].copy()
        if df.empty:
            return pd.DataFrame()
        # cover_size peut être stocké en string (csv) — on cast en numeric pour
        # éviter une division string/int.
        df['cover_size_num'] = pd.to_numeric(df['cover_size'], errors='coerce').fillna(0)
        df['cover_kb'] = df['cover_size_num'] / 1024
        cols = ['filepath', 'album', 'cover_size', 'cover_kb']
        available = [c for c in cols if c in df.columns]
        return df[available].sort_values('cover_size', ascending=False)
    
    def _audit_covers_invalid(self) -> pd.DataFrame:
        # Pochettes corrompues - integrite Pillow (cover_valid == No)
        if not self._has_cols('has_cover', 'cover_valid'):
            return pd.DataFrame()
        df = self.df[(self.df['has_cover'] == 'Yes') & (self.df['cover_valid'] == 'No')].copy()
        if df.empty:
            return pd.DataFrame()
        cols = ['filepath', 'parent_folder', 'album', 'cover_format', 'cover_error']
        available = [c for c in cols if c in df.columns]
        return df[available].sort_values('parent_folder') if 'parent_folder' in df.columns else df[available]

    def _audit_covers_too_small(self) -> pd.DataFrame:
        # Pochettes trop petites (< 300px, cf MIN_COVER_SIZE)
        if not self._has_cols('has_cover', 'cover_width', 'cover_height'):
            return pd.DataFrame()
        df = self.df[self.df['has_cover'] == 'Yes'].copy()
        if df.empty:
            return pd.DataFrame()
        df['w'] = pd.to_numeric(df['cover_width'], errors='coerce').fillna(0)
        df['h'] = pd.to_numeric(df['cover_height'], errors='coerce').fillna(0)
        df = df[(df['w'] > 0) & (df['h'] > 0) & ((df['w'] < 300) | (df['h'] < 300))]
        if df.empty:
            return pd.DataFrame()
        cols = ['filepath', 'album', 'cover_width', 'cover_height']
        available = [c for c in cols if c in df.columns]
        return df[available].sort_values('cover_width')

    def _audit_multiple_covers(self) -> pd.DataFrame:
        # F23(a) - Fichiers embarquant plusieurs images (cover_count > 1)
        if not self._has_cols('cover_count'):
            return pd.DataFrame()
        df = self.df.copy()
        df['cc_num'] = pd.to_numeric(df['cover_count'], errors='coerce').fillna(0)
        df = df[df['cc_num'] > 1]
        if df.empty:
            return pd.DataFrame()
        cols = ['filepath', 'parent_folder', 'album', 'cover_count']
        available = [c for c in cols if c in df.columns]
        return df[available].sort_values('parent_folder') if 'parent_folder' in df.columns else df[available]

    def _audit_duration_zero(self) -> pd.DataFrame:
        # Fichiers a duree nulle/quasi-nulle (tronques)
        if not self._has_cols('duration_seconds'):
            return pd.DataFrame()
        df = self.df.copy()
        df['dur_num'] = pd.to_numeric(df['duration_seconds'], errors='coerce').fillna(0)
        df = df[df['dur_num'] < 1]
        if df.empty:
            return pd.DataFrame()
        cols = ['filepath', 'album', 'duration', 'duration_seconds']
        available = [c for c in cols if c in df.columns]
        return df[available]

    def _audit_windows_path_issues(self) -> pd.DataFrame:
        # Caracteres illegaux Windows ou chemin > 240 (EZ CD / SMB)
        if not self._has_cols('filepath'):
            return pd.DataFrame()
        df = self.df.copy()
        df['path_len'] = df['filepath'].astype(str).str.len()
        fname = df['filepath'].astype(str).str.split('/').str[-1]
        illegal = fname.str.contains(r'[<>|?*:]', regex=True, na=False) | fname.str.contains(chr(34), regex=False, na=False)
        too_long = df['path_len'] > 240
        df = df[illegal | too_long]
        if df.empty:
            return pd.DataFrame()
        cols = ['filepath', 'album', 'path_len']
        available = [c for c in cols if c in df.columns]
        return df[available].sort_values('path_len', ascending=False)

    def _audit_cover_non_uniform(self) -> pd.DataFrame:
        """Covers non-uniformes"""
        # [26] Garde colonnes
        if not self._has_cols('album', 'cover_md5', 'parent_folder'):
            return pd.DataFrame()
        
        df = self.df[(self.df['album'] != '') & (self.df['cover_md5'] != '')].copy()
        issues = []
        for (folder, album), group in df.groupby(['parent_folder', 'album']):
            md5s = group['cover_md5'].unique()
            if len(md5s) > 1:
                issues.append({
                    'Dossier': folder,
                    'Album': album,
                    'MD5 différents': len(md5s),
                    'Fichiers': len(group)
                })
        return pd.DataFrame(issues)
    
    def _audit_codec_homogeneity(self) -> pd.DataFrame:
        """Homogénéité codec"""
        # [26] Garde colonnes
        if not self._has_cols('album', 'codec', 'parent_folder', 'disc'):
            return pd.DataFrame()
        
        df = self.df[self.df['album'] != ''].copy()
        df['disc_num'] = pd.to_numeric(df['disc'], errors='coerce').fillna(1).astype(int)  # T10 Lot E2
        issues = []
        for (folder, album, _disc), group in df.groupby(['parent_folder', 'album', 'disc_num']):
            codecs = group['codec'].unique()
            if len(codecs) > 1:
                issues.append({
                    'Dossier': folder,
                    'Album': album,
                    'Codecs': ', '.join(str(c) for c in codecs if c),
                    'Fichiers': len(group)
                })
        return pd.DataFrame(issues)
    
    def _audit_genre_stats(self) -> pd.DataFrame:
        """Statistiques genres"""
        # [26] Garde colonnes
        if not self._has_cols('genre'):
            return pd.DataFrame()
        
        df = self.df[self.df['genre'] != ''].copy()
        if df.empty:
            return pd.DataFrame()
        
        agg_dict = {'filename': 'count'}
        if 'size_mb' in df.columns:
            # [LOT v20-5-fix] coercion inline -- size_mb/duration_seconds TEXT
            # en SQLite, sum brut levait un TypeError.
            agg_dict['size_mb'] = lambda x: pd.to_numeric(x, errors='coerce').sum()
        if 'duration_seconds' in df.columns:
            agg_dict['duration_seconds'] = lambda x: pd.to_numeric(x, errors='coerce').sum()

        stats = df.groupby('genre').agg(agg_dict).reset_index()
        rename_map = {
            'genre': 'Genre', 'filename': 'Fichiers',
            'size_mb': 'Taille (MB)', 'duration_seconds': 'Durée (s)',
        }
        stats = stats.rename(columns=rename_map)
        if 'Durée (s)' in stats.columns:
            stats['Durée (h)'] = round(stats['Durée (s)'] / 3600, 2)
        return stats.sort_values('Fichiers', ascending=False)
    
    def _audit_case_inconsistency_artist(self) -> pd.DataFrame:
        """
        Détecte les incohérences de casse pour les albumartists.
        Ex: 'Mika' vs 'MIKA' vs 'mika' sur différents albums
        """
        if 'albumartist' not in self.df.columns:
            return pd.DataFrame()
        
        df = self.df[self.df['albumartist'] != ''].copy()
        if df.empty:
            return pd.DataFrame()
        
        # Groupe par version lowercase
        df['albumartist_lower'] = df['albumartist'].str.lower().str.strip()
        
        # Trouve les albumartists avec plusieurs variantes de casse
        variants = df.groupby('albumartist_lower')['albumartist'].apply(lambda x: list(x.unique())).reset_index()
        variants.columns = ['albumartist_lower', 'variantes']
        
        # Filtre ceux avec plus d'une variante
        variants = variants[variants['variantes'].apply(len) > 1]
        
        if variants.empty:
            return pd.DataFrame()
        
        # Construit le rapport détaillé
        results = []
        for _, row in variants.iterrows():
            variantes_list = row['variantes']
            
            for variante in variantes_list:
                subset = df[df['albumartist'] == variante]
                albums = subset['album'].unique() if 'album' in subset.columns else []
                
                results.append({
                    'AlbumArtist (saisi)': variante,
                    'AlbumArtist (normalisé)': row['albumartist_lower'],
                    'Autres variantes': ', '.join([v for v in variantes_list if v != variante]),
                    'Nb variantes total': len(variantes_list),
                    'Albums concernés': len(albums),
                    'Fichiers concernés': len(subset),
                    'Exemple album': albums[0] if len(albums) > 0 else ''
                })
        
        result_df = pd.DataFrame(results)
        return result_df.sort_values(['AlbumArtist (normalisé)', 'Fichiers concernés'], ascending=[True, False])
    
    def _audit_case_inconsistency_album(self) -> pd.DataFrame:
        """
        Détecte les incohérences de casse pour les noms d'albums.
        Ex: 'Abbey Road' vs 'ABBEY ROAD' vs 'abbey road'
        """
        if 'album' not in self.df.columns:
            return pd.DataFrame()
        
        df = self.df[self.df['album'] != ''].copy()
        if df.empty:
            return pd.DataFrame()
        
        # Groupe par version lowercase
        df['album_lower'] = df['album'].str.lower().str.strip()
        
        # Trouve les albums avec plusieurs variantes de casse
        variants = df.groupby('album_lower')['album'].apply(lambda x: list(x.unique())).reset_index()
        variants.columns = ['album_lower', 'variantes']
        
        # Filtre ceux avec plus d'une variante
        variants = variants[variants['variantes'].apply(len) > 1]
        
        if variants.empty:
            return pd.DataFrame()
        
        # Construit le rapport détaillé
        results = []
        for _, row in variants.iterrows():
            variantes_list = row['variantes']
            
            for variante in variantes_list:
                subset = df[df['album'] == variante]
                artists = subset['artist'].unique() if 'artist' in subset.columns else []
                
                results.append({
                    'Album (saisi)': variante,
                    'Album (normalisé)': row['album_lower'],
                    'Autres variantes': ', '.join([v for v in variantes_list if v != variante]),
                    'Nb variantes total': len(variantes_list),
                    'Artistes': ', '.join(list(artists)[:3]) + ('...' if len(artists) > 3 else ''),
                    'Fichiers concernés': len(subset)
                })
        
        result_df = pd.DataFrame(results)
        return result_df.sort_values(['Album (normalisé)', 'Fichiers concernés'], ascending=[True, False])
    
    def _audit_case_inconsistency_genre(self) -> pd.DataFrame:
        """
        Détecte les incohérences de casse pour les genres.
        Ex: 'Rock' vs 'ROCK' vs 'rock'
        """
        if 'genre' not in self.df.columns:
            return pd.DataFrame()
        
        df = self.df[self.df['genre'] != ''].copy()
        if df.empty:
            return pd.DataFrame()
        
        # Groupe par version lowercase
        df['genre_lower'] = df['genre'].str.lower().str.strip()
        
        # Trouve les genres avec plusieurs variantes de casse
        variants = df.groupby('genre_lower')['genre'].apply(lambda x: list(x.unique())).reset_index()
        variants.columns = ['genre_lower', 'variantes']
        
        # Filtre ceux avec plus d'une variante
        variants = variants[variants['variantes'].apply(len) > 1]
        
        if variants.empty:
            return pd.DataFrame()
        
        # Construit le rapport détaillé
        results = []
        for _, row in variants.iterrows():
            variantes_list = row['variantes']
            
            for variante in variantes_list:
                subset = df[df['genre'] == variante]
                
                results.append({
                    'Genre (saisi)': variante,
                    'Genre (normalisé)': row['genre_lower'],
                    'Autres variantes': ', '.join([v for v in variantes_list if v != variante]),
                    'Nb variantes total': len(variantes_list),
                    'Fichiers concernés': len(subset),
                    'Albums concernés': subset['album'].nunique() if 'album' in subset.columns else 0,
                })
        
        result_df = pd.DataFrame(results)
        return result_df.sort_values(['Genre (normalisé)', 'Fichiers concernés'], ascending=[True, False])
    
    def _audit_case_by_artist_album(self) -> pd.DataFrame:
        """
        Vue consolidée des incohérences de casse par AlbumArtist/Album.
        Montre pour chaque albumartist avec problème de casse, tous ses albums et fichiers.
        """
        if 'albumartist' not in self.df.columns or 'album' not in self.df.columns:
            return pd.DataFrame()
        
        df = self.df[self.df['albumartist'] != ''].copy()
        if df.empty:
            return pd.DataFrame()
        
        # Identifie les albumartists avec incohérences de casse
        df['albumartist_lower'] = df['albumartist'].str.lower().str.strip()
        
        # Trouve les albumartists avec plusieurs variantes
        artist_variants = df.groupby('albumartist_lower')['albumartist'].apply(lambda x: list(x.unique())).reset_index()
        artist_variants.columns = ['albumartist_lower', 'variantes']
        artist_variants = artist_variants[artist_variants['variantes'].apply(len) > 1]
        
        if artist_variants.empty:
            return pd.DataFrame()
        
        # Construit le rapport détaillé par albumartist/album
        results = []
        for _, row in artist_variants.iterrows():
            albumartist_lower = row['albumartist_lower']
            variantes_list = row['variantes']
            
            # Récupère tous les fichiers de cet albumartist (toutes variantes)
            artist_files = df[df['albumartist_lower'] == albumartist_lower]
            
            # Groupe par album
            for album, album_group in artist_files.groupby('album'):
                if album == '':
                    continue
                
                # Variantes utilisées dans cet album
                album_variants = album_group['albumartist'].unique().tolist()
                
                results.append({
                    'AlbumArtist (normalisé)': albumartist_lower,
                    'Variantes AlbumArtist': ' | '.join(variantes_list),
                    'Album': album,
                    'AlbumArtist dans album': ' | '.join(album_variants),
                    'Fichiers': len(album_group),
                    'Nb variantes utilisées': len(album_variants),
                    'Chemin': album_group['parent_folder'].iloc[0] if 'parent_folder' in album_group.columns else ''
                })
        
        result_df = pd.DataFrame(results)
        if result_df.empty:
            return pd.DataFrame()
        
        return result_df.sort_values(['AlbumArtist (normalisé)', 'Album'])
