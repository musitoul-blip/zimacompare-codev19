"""
tagaudit/core/db_health.py - Diagnostic profond de master_scan.db (LOT v20-7 B)

Complement de selfcheck.py (F19) : la ou selfcheck ne verifie que la
PRESENCE de master_scan.db, ce module inspecte son CONTENU (schema, index,
integrite SQLite, donnees, scan_meta, coherence avec le CSV fige).

Convention alignee sur selfcheck.py : chaque check isole dans son propre
try/except, statut ok/warn/fail, jamais d'exception qui remonte a l'appelant
-- un check qui plante degrade son propre statut, ne casse pas l'endpoint.

Connexion en lecture seule STRICTE (file:...?mode=ro, uri=True) : garantie
structurelle par SQLite lui-meme (pas seulement "on n'appelle pas commit"),
ce module ne doit jamais pouvoir ecrire dans master_scan.db. db.connect()
n'est donc PAS reutilise ici (il fait PRAGMA journal_mode=WAL, une ecriture,
incompatible avec mode=ro) -- seules les constantes de db.py sont importees.

Autonome comme db.py : import module-niveau limite a os/sqlite3 + la
constante db (leger, structurel). pandas (check 20), re et sys (parsing
schema / detection scan en cours) sont importes LOCALEMENT.
"""
import os
import sqlite3

from . import db

_ORDER = {"ok": 0, "warn": 1, "fail": 2}

EXPECTED_STATUS_DOMAIN = {"running", "completed", "paused", "failed"}
EXPECTED_HAS_COVER_DOMAIN = {"Yes", "No"}
EXPECTED_EXTENSIONS = {"mp3", "flac", "m4a"}
EXPECTED_SCHEMA_VERSION = 1  # doit rester synchrone avec le seed de db.init_schema()

# Colonnes numeriques connues (convention _num de audit_engine.py) -- fail
# reserve a size_mb/duration_seconds (bug .sum() deja vu 2x pendant la
# migration), warn pour les autres.
NUMERIC_COLUMNS_STRICT = ("size_mb", "duration_seconds")
NUMERIC_COLUMNS_SOFT = ("bitrate", "samplerate", "channels", "bitdepth",
                         "track", "tracktotal", "disc", "disctotal", "year",
                         "cover_size", "cover_width", "cover_height", "cover_count")

FRESHNESS_WARN_DAYS = 30

# Mesure reelle 2026-07-18 sur master_scan.db (47318 pistes) : album vide=0,
# albumartist vide=0. Seuil = plancher 15%, ou 2x le taux de reference si
# celui-ci est mis a jour un jour (ne se recalcule jamais tout seul sur les
# donnees live -- comparer un taux a 2x lui-meme n'aurait aucun sens).
BASELINE_EMPTY_RATE = 0.0
FILL_RATE_WARN_THRESHOLD = max(0.15, 2 * BASELINE_EMPTY_RATE)


def _c(cid, label, status, detail=""):
    return {"id": cid, "label": label, "status": status, "detail": detail}


def _parse_schema_columns(schema_sql, table_name):
    import re
    m = re.search(r'CREATE TABLE IF NOT EXISTS\s+' + re.escape(table_name) +
                   r'\s*\((.*?)\)\s*;', schema_sql, re.S)
    if not m:
        return []
    cols = []
    for line in m.group(1).split(','):
        line = line.strip()
        if line:
            cols.append(line.split()[0])
    return cols


def _parse_index_defs(indexes_list):
    import re
    result = {}
    for stmt in indexes_list:
        m = re.search(r'CREATE\s+(UNIQUE\s+)?INDEX\s+IF NOT EXISTS\s+(\S+)', stmt)
        if m:
            result[m.group(2)] = bool(m.group(1))
    return result


def _scan_in_progress():
    import sys
    try:
        if "/app" not in sys.path:
            sys.path.insert(0, "/app")
        from config import AppState, get_state
        return get_state().get("app_state") == AppState.SCANNING
    except Exception:
        return False


def db_health_check(db_path=None):
    checks = []
    path = db_path or db.DB_PATH

    try:
        conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
        conn.row_factory = sqlite3.Row
    except Exception as e:
        checks.append(_c("db_file", "Fichier master_scan.db", "fail", str(e)))
        return {"checks": checks, "verdict": "fail"}

    try:
        # ── STRUCTURE (6) ──────────────────────────────────────────────
        # 1
        try:
            size = os.path.getsize(path)
            checks.append(_c("db_file", "Fichier master_scan.db", "ok",
                             "%.1f Mo" % (size / 1024 / 1024)))
        except Exception as e:
            checks.append(_c("db_file", "Fichier master_scan.db", "fail", str(e)))

        # 2
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            checks.append(_c("journal_mode", "Mode journal WAL", "ok" if mode == "wal" else "warn", mode))
        except Exception as e:
            checks.append(_c("journal_mode", "Mode journal WAL", "warn", str(e)))

        # 3
        try:
            expected = set(_parse_schema_columns(db.SCHEMA, "tracks"))
            actual = set(r[1] for r in conn.execute("PRAGMA table_info(tracks)").fetchall())
            missing, extra = expected - actual, actual - expected
            if missing or extra:
                checks.append(_c("cols_tracks", "Colonnes table tracks", "fail",
                                 "manquantes=%s inattendues=%s" % (sorted(missing), sorted(extra))))
            else:
                checks.append(_c("cols_tracks", "Colonnes table tracks", "ok",
                                 "%d colonnes conformes au schema" % len(actual)))
        except Exception as e:
            checks.append(_c("cols_tracks", "Colonnes table tracks", "warn", str(e)))

        # 4
        try:
            expected = set(_parse_schema_columns(db.SCHEMA, "scan_meta"))
            actual = set(r[1] for r in conn.execute("PRAGMA table_info(scan_meta)").fetchall())
            missing, extra = expected - actual, actual - expected
            if missing or extra:
                checks.append(_c("cols_scan_meta", "Colonnes table scan_meta", "fail",
                                 "manquantes=%s inattendues=%s" % (sorted(missing), sorted(extra))))
            else:
                checks.append(_c("cols_scan_meta", "Colonnes table scan_meta", "ok",
                                 "%d colonnes conformes au schema" % len(actual)))
        except Exception as e:
            checks.append(_c("cols_scan_meta", "Colonnes table scan_meta", "warn", str(e)))

        # 5
        try:
            expected_idx = _parse_index_defs(db.INDEXES)
            actual_idx = {r[1]: bool(r[2]) for r in conn.execute("PRAGMA index_list(tracks)").fetchall()}
            missing = set(expected_idx) - set(actual_idx)
            if missing:
                checks.append(_c("indexes", "Index tracks presents", "fail",
                                 "manquants: %s" % sorted(missing)))
            else:
                checks.append(_c("indexes", "Index tracks presents", "ok",
                                 "%d index presents" % len(expected_idx)))
        except Exception as e:
            checks.append(_c("indexes", "Index tracks presents", "warn", str(e)))

        # 6
        try:
            expected_idx = _parse_index_defs(db.INDEXES)
            actual_idx = {r[1]: bool(r[2]) for r in conn.execute("PRAGMA index_list(tracks)").fetchall()}
            bad = [n for n, uniq in expected_idx.items()
                   if n in actual_idx and actual_idx[n] != uniq]
            if bad:
                checks.append(_c("index_unique", "Unicite index (filepath)", "fail",
                                 "unicite incorrecte: %s" % bad))
            else:
                checks.append(_c("index_unique", "Unicite index (filepath)", "ok",
                                 "idx_tracks_filepath unique=1"))
        except Exception as e:
            checks.append(_c("index_unique", "Unicite index (filepath)", "warn", str(e)))

        # ── INTEGRITE SQLite (4) ───────────────────────────────────────
        # 7
        try:
            r = conn.execute("PRAGMA integrity_check").fetchone()[0]
            checks.append(_c("integrity", "PRAGMA integrity_check", "ok" if r == "ok" else "fail", r))
        except Exception as e:
            checks.append(_c("integrity", "PRAGMA integrity_check", "fail", str(e)))

        # 8
        try:
            rows = conn.execute("PRAGMA foreign_key_check").fetchall()
            checks.append(_c("fk_check", "PRAGMA foreign_key_check", "ok" if not rows else "warn",
                             "" if not rows else "%d anomalies" % len(rows)))
        except Exception as e:
            checks.append(_c("fk_check", "PRAGMA foreign_key_check", "warn", str(e)))

        # 9
        try:
            size = os.path.getsize(path)
            n = conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
            incoherent = size < 1024 * 1024 and n > 1000
            checks.append(_c("db_size", "Taille fichier .db", "warn" if incoherent else "ok",
                             "%d octets, %d pistes" % (size, n)))
        except Exception as e:
            checks.append(_c("db_size", "Taille fichier .db", "warn", str(e)))

        # 10
        try:
            if _scan_in_progress():
                checks.append(_c("wal_size", "Taille -wal", "ok", "scan en cours, check ignore"))
            else:
                wal_path = path + "-wal"
                wal_size = os.path.getsize(wal_path) if os.path.exists(wal_path) else 0
                checks.append(_c("wal_size", "Taille -wal", "warn" if wal_size > 50 * 1024 * 1024 else "ok",
                                 "%d octets" % wal_size))
        except Exception as e:
            checks.append(_c("wal_size", "Taille -wal", "warn", str(e)))

        # ── CONTENU (10) ────────────────────────────────────────────────
        # 11
        try:
            n = conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
            checks.append(_c("row_count", "Nombre de pistes", "fail" if n == 0 else "ok", str(n)))
        except Exception as e:
            checks.append(_c("row_count", "Nombre de pistes", "fail", str(e)))

        # 12
        try:
            n = conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
            last = conn.execute("SELECT last_scan_count FROM scan_meta WHERE id=1").fetchone()
            lc = last[0] if last else None
            if lc is None:
                checks.append(_c("count_vs_meta", "Coherence COUNT(*) / scan_meta", "warn",
                                 "last_scan_count non renseigne"))
            elif lc != n:
                checks.append(_c("count_vs_meta", "Coherence COUNT(*) / scan_meta", "warn",
                                 "COUNT(*)=%d, last_scan_count=%d" % (n, lc)))
            else:
                checks.append(_c("count_vs_meta", "Coherence COUNT(*) / scan_meta", "ok", str(n)))
        except Exception as e:
            checks.append(_c("count_vs_meta", "Coherence COUNT(*) / scan_meta", "warn", str(e)))

        # 13
        try:
            n = conn.execute(
                "SELECT COUNT(*) FROM (SELECT filepath FROM tracks GROUP BY filepath HAVING COUNT(*)>1)"
            ).fetchone()[0]
            checks.append(_c("dup_filepath", "Doublons filepath", "fail" if n else "ok",
                             "%d doublons" % n if n else ""))
        except Exception as e:
            checks.append(_c("dup_filepath", "Doublons filepath", "warn", str(e)))

        # 14
        try:
            n = conn.execute("SELECT COUNT(*) FROM tracks WHERE filepath IS NULL OR filepath=''").fetchone()[0]
            checks.append(_c("empty_filepath", "Filepath vides/NULL", "fail" if n else "ok",
                             "%d lignes" % n if n else ""))
        except Exception as e:
            checks.append(_c("empty_filepath", "Filepath vides/NULL", "warn", str(e)))

        # 15
        try:
            vals = set(r[0] for r in conn.execute(
                "SELECT DISTINCT has_cover FROM tracks WHERE has_cover IS NOT NULL AND has_cover<>''"
            ).fetchall())
            bad = vals - EXPECTED_HAS_COVER_DOMAIN
            checks.append(_c("has_cover_domain", "Domaine has_cover", "fail" if bad else "ok",
                             "valeurs inattendues: %s" % sorted(bad) if bad else "%s" % sorted(vals)))
        except Exception as e:
            checks.append(_c("has_cover_domain", "Domaine has_cover", "warn", str(e)))

        # 16
        try:
            n = conn.execute(
                "SELECT COUNT(*) FROM tracks WHERE (title IS NULL OR title='') "
                "AND (artist IS NULL OR artist='') AND (album IS NULL OR album='') "
                "AND (albumartist IS NULL OR albumartist='')"
            ).fetchone()[0]
            checks.append(_c("ghost_rows", "Lignes fantomes (tags vides)", "warn" if n else "ok",
                             "%d lignes" % n if n else ""))
        except Exception as e:
            checks.append(_c("ghost_rows", "Lignes fantomes (tags vides)", "warn", str(e)))

        # 17
        try:
            row = conn.execute(
                "SELECT SUM(CASE WHEN album IS NULL OR album='' THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN albumartist IS NULL OR albumartist='' THEN 1 ELSE 0 END), "
                "COUNT(*) FROM tracks"
            ).fetchone()
            empty_album, empty_aa, total = row
            rate = max(empty_album or 0, empty_aa or 0) / total if total else 0
            checks.append(_c("tag_fill_rate", "Taux de remplissage album/albumartist",
                             "warn" if rate > FILL_RATE_WARN_THRESHOLD else "ok",
                             "album vide=%d, albumartist vide=%d / %d (seuil %.0f%%)"
                             % (empty_album, empty_aa, total, FILL_RATE_WARN_THRESHOLD * 100)))
        except Exception as e:
            checks.append(_c("tag_fill_rate", "Taux de remplissage album/albumartist", "warn", str(e)))

        # 18
        try:
            exts = {r[0]: r[1] for r in conn.execute(
                "SELECT extension, COUNT(*) FROM tracks GROUP BY extension").fetchall()}
            unknown = {e: c for e, c in exts.items() if e not in EXPECTED_EXTENSIONS}
            checks.append(_c("extensions", "Distribution extensions", "warn" if unknown else "ok",
                             "%s" % exts if not unknown else "inattendues: %s" % unknown))
        except Exception as e:
            checks.append(_c("extensions", "Distribution extensions", "warn", str(e)))

        # 19
        try:
            n = conn.execute("SELECT COUNT(*) FROM tracks WHERE error IS NOT NULL AND error<>''").fetchone()[0]
            checks.append(_c("error_rows", "Lignes en erreur de scan", "warn" if n else "ok",
                             "%d lignes" % n if n else ""))
        except Exception as e:
            checks.append(_c("error_rows", "Lignes en erreur de scan", "warn", str(e)))

        # 20 -- garde-fou anti-recidive du bug .sum() sur colonne TEXT
        try:
            import pandas as pd
            problems, warns = [], []
            for col in NUMERIC_COLUMNS_STRICT + NUMERIC_COLUMNS_SOFT:
                vals = [r[0] for r in conn.execute(
                    "SELECT %s FROM tracks WHERE %s IS NOT NULL AND %s<>''" % (col, col, col)
                ).fetchall()]
                if not vals:
                    continue
                bad = pd.to_numeric(pd.Series(vals), errors='coerce').isna().sum()
                if bad:
                    if col in NUMERIC_COLUMNS_STRICT:
                        problems.append("%s:%d" % (col, bad))
                    else:
                        warns.append("%s:%d" % (col, bad))
            if problems:
                checks.append(_c("numeric_cols", "Colonnes numeriques parsables", "fail",
                                 "non-numerique (critique): " + ", ".join(problems)))
            elif warns:
                checks.append(_c("numeric_cols", "Colonnes numeriques parsables", "warn",
                                 "non-numerique (secondaire): " + ", ".join(warns)))
            else:
                checks.append(_c("numeric_cols", "Colonnes numeriques parsables", "ok",
                                 "toutes coercibles"))
        except Exception as e:
            checks.append(_c("numeric_cols", "Colonnes numeriques parsables", "warn", str(e)))

        # ── SCAN_META (4) ──────────────────────────────────────────────
        # 21
        try:
            n = conn.execute("SELECT COUNT(*) FROM scan_meta WHERE id=1").fetchone()[0]
            checks.append(_c("scan_meta_row", "Ligne unique scan_meta", "fail" if n != 1 else "ok",
                             "id=1 %s" % ("present" if n == 1 else "absent/duplique")))
        except Exception as e:
            checks.append(_c("scan_meta_row", "Ligne unique scan_meta", "fail", str(e)))

        # 22
        try:
            r = conn.execute("SELECT last_scan_status FROM scan_meta WHERE id=1").fetchone()
            status = r[0] if r else None
            ok = status in EXPECTED_STATUS_DOMAIN
            checks.append(_c("scan_meta_status", "Domaine last_scan_status", "ok" if ok else "warn",
                             str(status)))
        except Exception as e:
            checks.append(_c("scan_meta_status", "Domaine last_scan_status", "warn", str(e)))

        # 23
        try:
            r = conn.execute("SELECT last_scan_status FROM scan_meta WHERE id=1").fetchone()
            status = r[0] if r else None
            checks.append(_c("scan_meta_failed", "Dernier scan pas en echec", "fail" if status == "failed" else "ok",
                             str(status)))
        except Exception as e:
            checks.append(_c("scan_meta_failed", "Dernier scan pas en echec", "warn", str(e)))

        # 24
        try:
            from datetime import datetime
            r = conn.execute("SELECT last_scan_completed FROM scan_meta WHERE id=1").fetchone()
            completed = r[0] if r else None
            if not completed:
                checks.append(_c("scan_freshness", "Fraicheur dernier scan", "warn", "jamais scanne"))
            else:
                age_days = (datetime.now() - datetime.fromisoformat(completed)).days
                checks.append(_c("scan_freshness", "Fraicheur dernier scan",
                                 "warn" if age_days > FRESHNESS_WARN_DAYS else "ok",
                                 "%d jours" % age_days))
        except Exception as e:
            checks.append(_c("scan_freshness", "Fraicheur dernier scan", "warn", str(e)))

        # ── CSV (3) ────────────────────────────────────────────────────
        # 25
        csv_path = os.path.splitext(path)[0] + ".csv"
        try:
            present = os.path.exists(csv_path)
            checks.append(_c("csv_present", "CSV fige present", "ok" if present else "warn",
                             csv_path if present else "absent: %s" % csv_path))
        except Exception as e:
            checks.append(_c("csv_present", "CSV fige present", "warn", str(e)))

        # 26
        try:
            with open(csv_path, encoding="utf-8", errors="replace") as f:
                csv_rows = sum(1 for _ in f) - 1  # header
            sql_rows = conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
            if sql_rows >= csv_rows:
                detail = ("csv=%d sql=%d -- CSV gele v20-6, ecart normal (+%d lignes)"
                          % (csv_rows, sql_rows, sql_rows - csv_rows))
            else:
                detail = "csv=%d sql=%d" % (csv_rows, sql_rows)
            checks.append(_c("csv_vs_sql", "Coherence CSV fige / SQL", "warn" if sql_rows < csv_rows else "ok",
                             detail))
        except Exception as e:
            checks.append(_c("csv_vs_sql", "Coherence CSV fige / SQL", "warn", str(e)))

        # 27
        try:
            r = conn.execute("SELECT schema_version FROM scan_meta WHERE id=1").fetchone()
            v = r[0] if r else None
            checks.append(_c("schema_version", "Version de schema", "ok" if v == EXPECTED_SCHEMA_VERSION else "warn",
                             "schema_version=%s (attendu %d)" % (v, EXPECTED_SCHEMA_VERSION)))
        except Exception as e:
            checks.append(_c("schema_version", "Version de schema", "warn", str(e)))

    finally:
        conn.close()

    verdict = "ok"
    for c in checks:
        if _ORDER[c["status"]] > _ORDER[verdict]:
            verdict = c["status"]
    return {"checks": checks, "verdict": verdict}
