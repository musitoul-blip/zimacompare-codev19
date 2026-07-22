"""
tagaudit/core/db.py - Base SQLite master_scan (migration v20, LOT v20-1)

Convention alignee sur core/audit_registry.py : sqlite3 brut (pas d'ORM),
row_factory = sqlite3.Row, DB_PATH env-overridable, CREATE TABLE/INDEX
idempotents. Module autonome, ne depend pas de core.config -- meme principe
d'independance que compressor.py/cover_routes.py (LOT 2b-1/LOT 3).

LOT v20-1 : schema seul. Rien n'alimente encore cette base -- le scanner
continue d'ecrire uniquement master_scan.csv. Le double ecriture arrive au
LOT v20-2.

Note WAL (a traiter au LOT v20-2, pas ici) : PRAGMA journal_mode=WAL cree
master_scan.db-wal et -shm ; toute lecture externe (regeneration CSV pour
la preuve d'equivalence) doit passer par un wal_checkpoint ou une connexion
fermee proprement avant lecture, sinon des ecritures recentes peuvent
manquer dans le fichier principal.
"""
import os
import sqlite3

DB_PATH = os.environ.get("ZIMA_MASTER_SCAN_DB", "/app_data/tagaudit/data/master_scan.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    filepath          TEXT, filename TEXT, extension TEXT, directory TEXT, parent_folder TEXT,
    size_mb           TEXT, modified_date TEXT, file_md5 TEXT, title TEXT, artist TEXT,
    album             TEXT, albumartist TEXT, composer TEXT, genre TEXT, year TEXT,
    track             TEXT, tracktotal TEXT, disc TEXT, disctotal TEXT, encoder TEXT,
    duration          TEXT, duration_seconds TEXT, bitrate TEXT, samplerate TEXT,
    channels          TEXT, bitdepth TEXT, codec TEXT, id3_version TEXT, has_cover TEXT,
    cover_size        TEXT, cover_format TEXT, cover_width TEXT, cover_height TEXT,
    cover_md5         TEXT, cover_valid TEXT, cover_error TEXT, cover_count TEXT, error TEXT
);
CREATE TABLE IF NOT EXISTS scan_meta (
    id                  INTEGER PRIMARY KEY CHECK (id = 1),
    last_scan_started   TEXT,
    last_scan_completed TEXT,
    last_scan_status    TEXT,
    last_scan_count     INTEGER,
    schema_version      INTEGER,
    last_scan_partial   INTEGER DEFAULT 0,
    last_scan_scope     TEXT DEFAULT ''
);
"""

INDEXES = [
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_tracks_filepath     ON tracks(filepath)",
    "CREATE INDEX IF NOT EXISTS idx_tracks_album               ON tracks(album)",
    "CREATE INDEX IF NOT EXISTS idx_tracks_albumartist         ON tracks(albumartist)",
    "CREATE INDEX IF NOT EXISTS idx_tracks_directory            ON tracks(directory)",
    "CREATE INDEX IF NOT EXISTS idx_tracks_cover_format         ON tracks(cover_format)",
    "CREATE INDEX IF NOT EXISTS idx_tracks_cover_md5            ON tracks(cover_md5)",
]


def connect(db_path=None):
    path = db_path or DB_PATH
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_schema(db_path=None):
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        for stmt in INDEXES:
            conn.execute(stmt)
        # [LOT marqueur base incomplete] migration douce -- meme motif que
        # audit_registry.py::_migrate() (ALTER TABLE si colonne absente).
        # Necessaire en plus du CREATE TABLE IF NOT EXISTS ci-dessus : celui-ci
        # ne migre pas une table scan_meta deja existante sur une base
        # deployee avant ce lot.
        cols = [r[1] for r in conn.execute("PRAGMA table_info(scan_meta)").fetchall()]
        if 'last_scan_partial' not in cols:
            conn.execute("ALTER TABLE scan_meta ADD COLUMN last_scan_partial INTEGER DEFAULT 0")
        if 'last_scan_scope' not in cols:
            conn.execute("ALTER TABLE scan_meta ADD COLUMN last_scan_scope TEXT DEFAULT ''")
        # [LOT v20-7a] Seed idempotent de la ligne unique scan_meta -- ne
        # s'insere que si absente, n'ecrase jamais un etat deja present
        # (schema_version/last_scan_* survivent aux appels repetes).
        conn.execute("INSERT OR IGNORE INTO scan_meta (id, schema_version) VALUES (1, 1)")
        conn.commit()
    finally:
        conn.close()


_scan_meta_cache = None


def get_scan_meta_cached(db_path=None):
    """Lit scan_meta une fois, garde le resultat en cache module. Renvoie
    None si la lecture echoue -- le front n'affiche rien plutot que de
    fausses alertes."""
    global _scan_meta_cache
    if _scan_meta_cache is not None:
        return _scan_meta_cache
    try:
        conn = connect(db_path)
        try:
            row = conn.execute(
                "SELECT last_scan_status, last_scan_completed, last_scan_count, "
                "last_scan_partial, last_scan_scope FROM scan_meta WHERE id=1"
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        _scan_meta_cache = dict(row)
        return _scan_meta_cache
    except Exception:
        return None


def invalidate_scan_meta_cache():
    """Remet le cache scan_meta a None -- a appeler apres toute ecriture de
    scan_meta (fin de scan)."""
    global _scan_meta_cache
    _scan_meta_cache = None
