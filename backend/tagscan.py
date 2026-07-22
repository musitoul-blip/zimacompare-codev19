"""backend/tagscan.py - lanceur du scan-tag (v9).
Reutilise BackgroundScanner (porte verbatim dans tagaudit/) pour produire
master_scan.csv. Progression via app_state. Supporte selection de formats,
filtre par nom de dossier, et limite de fichiers. Un seul job a la fois.
"""
import os
import sys
import time
import threading
from pathlib import Path

if "/app/tagaudit" not in sys.path:
    sys.path.insert(0, "/app/tagaudit")

from config import AppState, get_state, update_state

TAG_SOURCE_DEFAULT = "/disks/HDD-Storage1/Media/GoogleMusic"

_scanner = None
_lock = threading.Lock()
_meta = {"started_at": 0.0, "ended_at": 0.0}
_dir_cache = {"source": None, "built_at": 0.0, "dirs": [], "sig": None}


def _source_signature(source):
    """Snapshot leger de la source pour invalider le cache des dossiers.
    (mtime racine, nb d'entrees niveau 1, somme des mtime des sous-dossiers
    niveau 1). La somme des mtime attrape un ajout/retrait DANS un album
    existant (son mtime de dossier change), pas seulement a la racine.
    Borne : un stat par sous-dossier (rapide). Retourne None si illisible."""
    try:
        root_mtime = os.stat(source).st_mtime
    except OSError:
        return None
    count = 0
    sum_mtime = 0.0
    try:
        with os.scandir(source) as it:
            for e in it:
                count += 1
                if e.is_dir(follow_symlinks=False):
                    try:
                        sum_mtime += e.stat(follow_symlinks=False).st_mtime
                    except OSError:
                        pass
    except OSError:
        return None
    return (round(root_mtime, 3), count, round(sum_mtime, 3))


def list_source_dirs(source=None, refresh=False):
    """Index des sous-dossiers immediats de la source + comptage par format.
    Mis en cache (par source). Alimente le filtre interactif cote UI."""
    source = source or TAG_SOURCE_DEFAULT
    cur_sig = _source_signature(source)
    if (not refresh and _dir_cache["source"] == source and _dir_cache["dirs"]
            and _dir_cache.get("sig") == cur_sig and cur_sig is not None):
        return _dir_cache
    dirs = []
    try:
        entries = sorted((e for e in os.scandir(source) if e.is_dir()),
                         key=lambda e: e.name.lower())
    except OSError:
        entries = []
    for e in entries:
        c = {"mp3": 0, "flac": 0, "m4a": 0}
        for root, _d, files in os.walk(e.path):
            for fn in files:
                ext = os.path.splitext(fn)[1].lower()
                if ext == ".mp3":
                    c["mp3"] += 1
                elif ext == ".flac":
                    c["flac"] += 1
                elif ext == ".m4a":
                    c["m4a"] += 1
        tot = c["mp3"] + c["flac"] + c["m4a"]
        if tot:
            dirs.append({"name": e.name, "mp3": c["mp3"], "flac": c["flac"],
                         "m4a": c["m4a"], "total": tot})
    _dir_cache.update(source=source, built_at=time.time(), dirs=dirs, sig=cur_sig)
    return _dir_cache


def dirs_payload(source=None, refresh=False):
    idx = list_source_dirs(source, refresh)
    dirs = idx["dirs"]
    by = {"mp3": sum(d["mp3"] for d in dirs),
          "flac": sum(d["flac"] for d in dirs),
          "m4a": sum(d["m4a"] for d in dirs)}
    return {"source": idx["source"], "count": len(dirs),
            "total_files": by["mp3"] + by["flac"] + by["m4a"],
            "by_format": by, "built_at": idx["built_at"], "dirs": dirs}


def _exts_from(formats):
    if not formats:
        return None
    out = set()
    for f in formats:
        f = str(f).lower().lstrip(".")
        if f in ("mp3", "flac", "m4a"):
            out.add("." + f)
    return out or None


def _run(scan_paths, exts, limit, is_partial, scope):
    global _scanner
    try:
        from engine.scanner import BackgroundScanner
        sc = BackgroundScanner(scan_paths, formats=exts, file_limit=limit,
                                is_partial=is_partial, scope=scope)
        with _lock:
            _scanner = sc
        sc.start()
    except Exception as e:
        update_state(app_state=AppState.ERROR, error="scan-tag: %s" % e)
    finally:
        _meta["ended_at"] = time.time()
        with _lock:
            _scanner = None


def start_tag_scan(source=None, formats=None, name_filter=None, limit=None):
    """Demarre un scan-tag. Retourne True, ou 'busy' / 'nomatch'."""
    source = source or TAG_SOURCE_DEFAULT
    state = get_state()
    if state["app_state"] not in (AppState.IDLE, AppState.ERROR):
        return "busy"
    nf = (name_filter or "").strip().lower()
    if nf:
        idx = list_source_dirs(source)
        sel = [os.path.join(source, d["name"]) for d in idx["dirs"]
               if nf in d["name"].lower()]
        if not sel:
            return "nomatch"
        scan_paths = [Path(p) for p in sel]
    else:
        scan_paths = [Path(source)]
    exts = _exts_from(formats)
    try:
        lim = int(limit) if limit else None
        if lim is not None and lim <= 0:
            lim = None
    except (TypeError, ValueError):
        lim = None
    # [LOT marqueur base incomplete] is_partial/scope -- les 3 sources de
    # partialite (dossiers filtres, formats restreints, limite de fichiers).
    is_partial = bool(nf) or exts is not None or lim is not None
    parties = []
    if nf:
        parties.append('dossiers "%s"' % nf)
    if exts:
        parties.append('formats %s' % ','.join(sorted(exts)))
    if lim:
        parties.append('limite %d fichiers' % lim)
    scope = ' + '.join(parties) if is_partial else 'complet'
    _meta["started_at"] = time.time()
    _meta["ended_at"] = 0.0
    update_state(app_state=AppState.SCANNING, method="tagscan", source=source,
                 target="", error="", scan_done=False, progress=0, processed=0,
                 total=0, current_file="Pre-scan...", fps=0, eta_seconds=0,
                 new_count=0, different_count=0, deleted_count=0, identical_count=0)
    threading.Thread(target=_run, args=(scan_paths, exts, lim, is_partial, scope), daemon=True).start()
    return True


def stop_tag_scan():
    with _lock:
        sc = _scanner
    if sc is not None:
        sc.stop()
        return True
    return False


def tag_result_info():
    """[LOT v20-5c] Statistiques depuis la table SQLite tracks (master_scan.db).

    _master_csv_path() supprimee (aucun autre appelant, verifie par grep) --
    repliee directement ici. Simplification actee : l'ancien double-repli
    CSV (parsing structure -> comptage brut de lignes si echec) n'a plus de
    sens sur une requete SQL bien formee -- une exception ici est une vraie
    erreur (base corrompue, schema absent), pas un cas a masquer
    silencieusement derriere un comptage degrade.
    """
    if "/app/tagaudit" not in sys.path:
        sys.path.insert(0, "/app/tagaudit")
    from core import db

    db_path = Path(db.DB_PATH)
    if not db_path.exists():
        return {"exists": False, "rows": 0, "path": str(db_path)}

    by = {"mp3": 0, "flac": 0, "m4a": 0, "autre": 0}
    conn = db.connect()
    try:
        rows = conn.execute("SELECT COUNT(*) AS n FROM tracks").fetchone()["n"]
        for row in conn.execute("SELECT extension, COUNT(*) AS n FROM tracks GROUP BY extension"):
            ext = (row["extension"] or "").lower()
            if ext in by:
                by[ext] += row["n"]
            else:
                by["autre"] += row["n"]
    finally:
        conn.close()

    dur = 0.0
    if _meta["ended_at"] and _meta["started_at"] and _meta["ended_at"] > _meta["started_at"]:
        dur = round(_meta["ended_at"] - _meta["started_at"], 1)
    fps = round(rows / dur, 1) if dur > 0 else 0
    return {"exists": True, "rows": rows, "path": str(db_path),
            "mtime": db_path.stat().st_mtime, "by_format": by,
            "duration_seconds": dur, "fps_avg": fps}


def tag_progress():
    from config import get_state
    st = get_state()
    fmt = {}
    try:
        from core import state_manager as _sm
        fmt = _sm.fmt_counts()
    except Exception:
        fmt = {}
    return {"app_state": st.get("app_state"), "method": st.get("method"),
            "processed": st.get("processed", 0), "total": st.get("total", 0),
            "progress": st.get("progress", 0), "fps": st.get("fps", 0),
            "eta_seconds": st.get("eta_seconds", 0),
            "current_file": st.get("current_file", ""), "fmt": fmt}


def build_tag_export():
    """Genere l'Excel d'audit ZimaTAG depuis master_scan.csv (excel_export 2.5.1,
    verbatim). Retourne le chemin du .xlsx. Bloque si une operation est en cours."""
    state = get_state()
    if state["app_state"] not in (AppState.IDLE, AppState.ERROR):
        raise RuntimeError("Operation en cours -- attendez la fin du scan-tag")
    if "/app/tagaudit" not in sys.path:
        sys.path.insert(0, "/app/tagaudit")
    from export import export_to_excel
    from core import config as tagcfg
    import shutil
    src = export_to_excel()
    data_dir = str(tagcfg.DATA_DIR)
    dest = os.path.join(data_dir, os.path.basename(src))
    try:
        if os.path.abspath(src) != os.path.abspath(dest):
            shutil.copy2(src, dest)
            sub = os.path.dirname(src)
            if (os.path.abspath(sub) != os.path.abspath(data_dir)
                    and os.path.basename(sub).startswith("ZimaTAG_Audit_")):
                shutil.rmtree(sub, ignore_errors=True)
    except Exception:
        dest = src
    return str(dest)


def build_tag_report_html():
    # F15: genere le rapport HTML d'audit ZimaTAG (string). Bloque si operation en cours.
    state = get_state()
    if state['app_state'] not in (AppState.IDLE, AppState.ERROR):
        raise RuntimeError('Operation en cours -- attendez la fin du scan-tag')
    if '/app/tagaudit' not in sys.path:
        sys.path.insert(0, '/app/tagaudit')
    from export import export_to_html
    return export_to_html()
