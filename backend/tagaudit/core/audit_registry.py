# -*- coding: utf-8 -*-
"""T10 Lot F — base SQLite `audit_registry` (source unique de config des audits).

Une ligne par audit. Colonnes humaines (onglet/classement/poids/health/actif/
decision/note) editables depuis l'app. Le moteur LIT cette base au lieu des
constantes codees en dur (HEALTH_WEIGHTS / INFO_KEYS / SHEET_GROUPS).

Seed initial = etat post-E2 (avec correction covers_bluesound_oversized -> INFO).
"""
import os
import json
import sqlite3
from datetime import datetime, timezone

# Emplacement persistant (volume monte). Override possible pour les tests.
DB_PATH = os.environ.get("ZIMA_AUDIT_REGISTRY_DB", "/app_data/audit_registry.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_registry (
    audit_key        TEXT PRIMARY KEY,
    libelle          TEXT,
    onglet_cible     TEXT,
    classement_cible TEXT,
    dans_health      INTEGER DEFAULT 0,
    poids_cible      REAL    DEFAULT 0.0,
    actif            INTEGER DEFAULT 1,
    ordre            INTEGER DEFAULT 0,
    decision         TEXT    DEFAULT '',
    note             TEXT    DEFAULT '',
    par_dossier      INTEGER DEFAULT 0,  -- T10 Lot G1
    updated_at       TEXT
);
"""

# --- SEED : etat post-E2. (libelle, audit_key) par groupe, ordre preserve. ---
# Correction T10 Lot F : covers_bluesound_oversized classement probleme -> INFO.
_SEED_GROUPS = [
    ("cockpit", [("🎯 Cockpit", "cockpit")]),
    ("kpi", [
        ("📊 KPI Global", "kpi_dashboard"),
        ("📅 KPI Années", "kpi_years"),
        ("🎵 KPI Genres", "kpi_genres"),
        ("👤 KPI Artistes", "kpi_albumartists"),
    ]),
    ("qualite", [
        ("🎧 Qualité Audio", "quality_analysis"),
        ("🔀 Bitrate mixte/album", "bitrate_mixed_album"),
        ("🔊 Incohér. Samplerate", "samplerate_inconsistency"),
        ("🆔 Version ID3 mixte", "id3_version_inconsistency"),
        ("📀 Homogénéité Codec", "codec_homogeneity"),
        ("⏱️ Durée nulle", "duration_zero"),
    ]),
    ("integrite", [
        ("📦 Albums Incomplets", "incomplete_albums"),
        ("🔢 Trous Numérotation", "track_gaps"),
        ("📝 Écarts Album", "album_gaps"),
        ("📋 Écarts Détaillés", "album_gaps_detailed"),
    ]),
    ("metadonnees", [
        ("🏷️ Tags Manquants", "missing_metadata"),
        ("🔣 Mojibake", "mojibake"),
        ("🚫 Sans Genre", "missing_genre_albums"),
        ("📆 Sans Année", "missing_year_albums"),
        ("⚠️ Années Invalides", "invalid_year_format"),
        ("👥 Cohér. Album Artist", "albumartist_consistency"),
        ("💿 Cohér. Nom Album", "album_name_consistency"),
        ("🎭 Incohér. Genre", "genre_inconsistency"),
        ("✏️ Typo AlbumArtist", "albumartist_typo"),
        ("📂 Dossier ≠ AlbumArtist", "folder_artist_mismatch"),
    ]),
    ("doublons", [
        ("🔍 Doublons MD5", "duplicates_md5"),
        ("🎤 Doublons Titre", "duplicates_artist_title"),
    ]),
    ("casse", [
        ("🔠 Casse AlbumArtist", "case_inconsistency_artist"),
        ("🔡 Casse Albums", "case_inconsistency_album"),
        ("🔤 Casse Genres", "case_inconsistency_genre"),
        ("📋 Casse AlbumArtist-Album", "case_by_artist_album"),
    ]),
    ("images", [
        ("🎨 Covers Non-Uniformes", "cover_non_uniform"),
        ("🚫 Pochettes non-JPG", "covers_non_jpg"),
        ("❌ Pochettes corrompues", "covers_invalid"),
        ("🔍 Pochettes trop petites", "covers_too_small"),
        ("🖼️ Images multiples", "multiple_covers"),
    ]),
    ("donnees", [
        ("📁 Données Complètes", "music_tags"),
        ("🪟 Chemins Windows", "windows_path_issues"),
    ]),
    ("informations", [
        ("👤 Artist ≠ AlbumArtist", "albumartist_vs_artist"),
        ("⚡ Anomalies Bitrate", "bitrate_anomalies"),
        ("🖼️ Taille Pochettes", "cover_size"),
        ("📺 Pochettes > Bluesound", "covers_bluesound_oversized"),
        ("📈 Stats Genres", "genre_stats"),
        ("📅 Incohér. Année", "year_inconsistency"),
    ]),
]

# Poids health (etat post-E2 = post Lot A).
_SEED_WEIGHTS = {
    "duplicates_md5": 3.0, "missing_metadata": 2.5, "incomplete_albums": 2.0,
    "track_gaps": 1.5, "samplerate_inconsistency": 1.0, "invalid_year_format": 1.5,
    "genre_inconsistency": 0.8, "albumartist_consistency": 1.2,
    "missing_genre_albums": 1.0, "missing_year_albums": 1.0,
    "case_inconsistency_artist": 0.5,
    "case_inconsistency_genre": 0.5, "cover_non_uniform": 0.3,  # multiple_covers harmonise INFO (retire)
    "mojibake": 0.8,
    # NB T10 Lot A : case_inconsistency_album mis a 0.0 (absent ici = 0.0 par defaut).
    # poids 0 explicites (Lot A + autres) : non listes = 0.0 par defaut
}

# Classements (etat post-E2 + correction bluesound -> INFO).
_SEED_KPI = {"kpi_dashboard", "kpi_years", "kpi_genres", "kpi_albumartists", "genre_stats"}
_SEED_SKIP = {"music_tags"}
# T10 Lot G1 : audits INFO repeches dans la vue "Par dossier" (ex-PARDOSSIER_KEEP)
_SEED_PARDOSSIER = {"bitrate_mixed_album", "id3_version_inconsistency", "albumartist_typo", "folder_artist_mismatch"}
# T10 Lot I1 : parametres metier editables (seuils). (param_key, value, audit_key, label, unit)
_SEED_PARAMS = [
    ("bluesound_max_kb", 700.0, "covers_bluesound_oversized",
     "Seuil poids pochette Bluesound", "Ko"),
    ("bluesound_resize_px", 600.0, "covers_bluesound_oversized",
     "Cible de redimensionnement pochette Bluesound (Mp3tag)", "px"),
]

# --- Lot 1 BluOS : paramètres du scanner d'artwork Bluesound (table séparée bluos_config) ---
# NB: value stockée en TEXT (contrairement à audit_params en REAL) car bluos_ip est une chaîne.
#     Les appelants castent en int/float pour les seuils numériques.
# Tuples : (param_key, value, label, unit)
_SEED_BLUOS = [
    ("bluos_ip", "192.168.1.121", "IP du lecteur Bluesound (Node)", ""),
    ("bluos_source_path", "/disks/HDD-Storage1/Media/GoogleMusic", "Dossier local a diagnostiquer (volet B)", ""),
    ("bluos_port", "11000", "Port API BluOS", ""),
    ("bluos_timeout", "10", "Timeout requête BluOS", "s"),
    ("embedded_max_kb", "600", "Pochette embarquée : taille max", "Ko"),
    ("external_autoresize_min_kb", "600", "Pochette externe : redim. auto au-delà", "Ko"),
    ("external_autoresize_max_kb", "4096", "Pochette externe : taille max acceptée", "Ko"),
    ("external_no_optimize_max_px", "1200", "Pochette externe : dimension max si optim. off", "px"),
]

# Parametres devenus obsoletes (fix placeholder par md5, 2026-07-22).
# Masques de l'UI : ils ne pilotent plus rien de reglable.
# placeholder_min_count : plus aucun consommateur.
# placeholder_max_kb : ne gouverne plus que le log de decouvrabilite
#   de bluos_scanner (lu via get_bluos_param, defaut 50).
# Les lignes existantes restent en base, inertes -- aucun DELETE.
_DEPRECATED_BLUOS = {"placeholder_min_count", "placeholder_max_kb"}
_SEED_INFO = {
    "cover_size", "quality_analysis", "albumartist_vs_artist", "duplicates_artist_title",
    "bitrate_mixed_album", "id3_version_inconsistency", "albumartist_typo",
    "folder_artist_mismatch", "album_name_consistency", "bitrate_anomalies",
    "case_inconsistency_album",
    "covers_bluesound_oversized",  # T10 Lot F: correction probleme -> INFO
    "multiple_covers",  # rattache T10: harmonise INFO comme F17/F21/F22 (filet 0 cas)
}

def _classement(key):
    if key in _SEED_SKIP: return "SKIP"
    if key in _SEED_KPI:  return "KPI"
    if key in _SEED_INFO: return "INFO"
    return "probleme"

def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def connect(db_path=None):
    path = db_path or DB_PATH
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn

def _migrate(conn):
    """T10 Lot G1 : migration douce - ajoute par_dossier si absente, peuple les 4 INFO repeches."""
    # T10 Lot H1 : table des preferences UI (cle/valeur JSON, ex. largeurs de colonnes)
    conn.execute("CREATE TABLE IF NOT EXISTS ui_prefs (pref_key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)")
    # T10 Lot I1 : table des parametres metier (seuils editables)
    conn.execute("CREATE TABLE IF NOT EXISTS audit_params (param_key TEXT PRIMARY KEY, value REAL, audit_key TEXT, label TEXT, unit TEXT, updated_at TEXT)")
    for _pk, _val, _ak, _lbl, _un in _SEED_PARAMS:
        conn.execute("INSERT OR IGNORE INTO audit_params (param_key, value, audit_key, label, unit, updated_at) VALUES (?,?,?,?,?,?)",
                     (_pk, _val, _ak, _lbl, _un, _now()))
    conn.commit()
    # --- Lot 1 BluOS : table bluos_config (paramètres du scanner, value TEXT) ---
    conn.execute("CREATE TABLE IF NOT EXISTS bluos_config (param_key TEXT PRIMARY KEY, value TEXT, label TEXT, unit TEXT, updated_at TEXT)")
    for _bk, _bval, _blbl, _bun in _SEED_BLUOS:
        conn.execute("INSERT OR IGNORE INTO bluos_config (param_key, value, label, unit, updated_at) VALUES (?,?,?,?,?)",
                     (_bk, _bval, _blbl, _bun, _now()))
    conn.commit()
    cols = [r[1] for r in conn.execute("PRAGMA table_info(audit_registry)").fetchall()]
    if 'par_dossier' not in cols:
        conn.execute("ALTER TABLE audit_registry ADD COLUMN par_dossier INTEGER DEFAULT 0")
        qs = ','.join('?' * len(_SEED_PARDOSSIER))
        conn.execute("UPDATE audit_registry SET par_dossier=1 WHERE audit_key IN (%s)" % qs,
                     tuple(_SEED_PARDOSSIER))
        conn.commit()

def init_and_seed(db_path=None, force=False):
    """Cree la table si absente et la seed si vide (ou force=True). Idempotent."""
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)  # T10 Lot G1 : migration avant le check n>0
        cur = conn.execute("SELECT COUNT(*) AS n FROM audit_registry")
        n = cur.fetchone()["n"]
        if n > 0 and not force:
            return False  # deja seedee
        if force:
            conn.execute("DELETE FROM audit_registry")
        ordre = 0
        ts = _now()
        for group, items in _SEED_GROUPS:
            for libelle, key in items:
                poids = _SEED_WEIGHTS.get(key, 0.0)
                conn.execute(
                    "INSERT OR REPLACE INTO audit_registry "
                    "(audit_key, libelle, onglet_cible, classement_cible, dans_health, "
                    " poids_cible, actif, ordre, decision, note, par_dossier, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (key, libelle, group, _classement(key), 1 if poids > 0 else 0,
                     poids, 1, ordre, "", "", 1 if key in _SEED_PARDOSSIER else 0, ts),
                )
                ordre += 1
        conn.commit()
        return True
    finally:
        conn.close()

# --- Accesseurs pour le moteur (F2 : remplacent les constantes) ---
def get_all(db_path=None):
    conn = connect(db_path)
    try:
        rows = conn.execute("SELECT * FROM audit_registry ORDER BY ordre").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()

def get_health_weights(db_path=None):
    return {r["audit_key"]: r["poids_cible"] for r in get_all(db_path)}

def get_info_keys(db_path=None):
    return {r["audit_key"] for r in get_all(db_path) if r["classement_cible"] == "INFO"}

def get_kpi_keys(db_path=None):
    return {r["audit_key"] for r in get_all(db_path) if r["classement_cible"] == "KPI"}

def get_skip_keys(db_path=None):
    return {r["audit_key"] for r in get_all(db_path) if r["classement_cible"] == "SKIP"}

def get_pardossier_keep(db_path=None):  # T10 Lot G1
    return {r["audit_key"] for r in get_all(db_path) if r.get("par_dossier")}

def get_sheet_groups(db_path=None):
    """Reconstitue SHEET_GROUPS {groupe: [(libelle, key), ...]} dans l'ordre."""
    groups = {}
    for r in get_all(db_path):
        groups.setdefault(r["onglet_cible"], []).append((r["libelle"], r["audit_key"]))
    return groups

def export_json(db_path=None):
    return json.dumps(get_all(db_path), ensure_ascii=False, indent=2)

def update_row(audit_key, fields, db_path=None):
    """Met a jour les champs humains d'une ligne. fields = dict de colonnes."""
    allowed = {"libelle", "onglet_cible", "classement_cible", "dans_health",
               "poids_cible", "actif", "ordre", "decision", "note", "par_dossier"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return False
    sets["updated_at"] = _now()
    cols = ", ".join("%s = ?" % k for k in sets)
    vals = list(sets.values()) + [audit_key]
    conn = connect(db_path)
    try:
        cur = conn.execute("UPDATE audit_registry SET %s WHERE audit_key = ?" % cols, vals)
        conn.commit()
        return cur.rowcount > 0  # rowcount check F3 : 0 si audit_key inconnu -> False
    finally:
        conn.close()

# --- T10 Lot H1 : preferences UI (cle/valeur JSON) ---
def get_ui_pref(key, db_path=None):
    """Retourne la valeur JSON decodee d'une preference UI, ou None si absente."""
    conn = connect(db_path)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS ui_prefs (pref_key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)")
        row = conn.execute("SELECT value FROM ui_prefs WHERE pref_key = ?", (key,)).fetchone()
        if not row:
            return None
        try:
            return json.loads(row["value"])
        except Exception:
            return None
    finally:
        conn.close()

def set_ui_pref(key, value, db_path=None):
    """Stocke une preference UI (value serialisee en JSON). Idempotent (upsert)."""
    conn = connect(db_path)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS ui_prefs (pref_key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)")
        conn.execute("INSERT OR REPLACE INTO ui_prefs (pref_key, value, updated_at) VALUES (?,?,?)",
                     (key, json.dumps(value, ensure_ascii=False), _now()))
        conn.commit()
        return True
    finally:
        conn.close()

# --- T10 Lot I1 : parametres metier d'audits (seuils editables) ---
def get_audit_param(key, default=None, db_path=None):
    """Retourne la valeur (float) d'un parametre, ou default si absent."""
    conn = connect(db_path)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS audit_params (param_key TEXT PRIMARY KEY, value REAL, audit_key TEXT, label TEXT, unit TEXT, updated_at TEXT)")
        row = conn.execute("SELECT value FROM audit_params WHERE param_key = ?", (key,)).fetchone()
        return row["value"] if row else default
    finally:
        conn.close()

def get_all_audit_params(db_path=None):
    """Liste tous les parametres (dicts) pour l'UI."""
    conn = connect(db_path)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS audit_params (param_key TEXT PRIMARY KEY, value REAL, audit_key TEXT, label TEXT, unit TEXT, updated_at TEXT)")
        rows = conn.execute("SELECT param_key, value, audit_key, label, unit, updated_at FROM audit_params ORDER BY audit_key, param_key").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()

def set_audit_param(key, value, db_path=None):
    """Met a jour la valeur d'un parametre existant. False si param_key inconnu."""
    conn = connect(db_path)
    try:
        cur = conn.execute("UPDATE audit_params SET value = ?, updated_at = ? WHERE param_key = ?",
                           (float(value), _now(), key))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# --- Lot 1 BluOS : accesseurs bluos_config (copie du motif audit_params, value TEXT) ---
def get_bluos_param(key, default=None, db_path=None):
    """Retourne la valeur (str) d'un paramètre BluOS, ou default si absent."""
    conn = connect(db_path)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS bluos_config (param_key TEXT PRIMARY KEY, value TEXT, label TEXT, unit TEXT, updated_at TEXT)")
        row = conn.execute("SELECT value FROM bluos_config WHERE param_key = ?", (key,)).fetchone()
        return row["value"] if row else default
    finally:
        conn.close()

def get_all_bluos_params(db_path=None):
    """Liste tous les parametres BluOS (dicts) pour l'UI. Filtre les cles
    devenues obsoletes (_DEPRECATED_BLUOS) -- restent en base, inertes,
    mais masquees de l'UI (aucun DELETE)."""
    conn = connect(db_path)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS bluos_config (param_key TEXT PRIMARY KEY, value TEXT, label TEXT, unit TEXT, updated_at TEXT)")
        rows = conn.execute("SELECT param_key, value, label, unit, updated_at FROM bluos_config ORDER BY param_key").fetchall()
        return [dict(r) for r in rows if r["param_key"] not in _DEPRECATED_BLUOS]
    finally:
        conn.close()

def set_bluos_param(key, value, db_path=None):
    """Met a jour la valeur (str) d'un paramètre BluOS existant. False si param_key inconnu."""
    conn = connect(db_path)
    try:
        cur = conn.execute("UPDATE bluos_config SET value = ?, updated_at = ? WHERE param_key = ?",
                           (str(value), _now(), key))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()
