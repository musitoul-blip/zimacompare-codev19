"""F19 - Auto-diagnostic d'integrite runtime (carte Verification).

Checks LECTURE SEULE, rapides, sans scan FUSE. Complement runtime du
bugcheck.sh (qui garde les linters statiques cote dev). Chaque check est
isole dans un try/except : un check qui rate degrade son statut sans
casser l'endpoint.
"""
import os
import re
import sys
import importlib
import compileall
from pathlib import Path

_ORDER = {"ok": 0, "warn": 1, "fail": 2}


def _c(cid, label, status, detail=""):
    return {"id": cid, "label": label, "status": status, "detail": detail}


def _master_db_for_selfcheck():
    """[LOT v20-6] Chemin master_scan.db via core.db, fallback en dur (T1)."""
    p = "/app_data/tagaudit/data/master_scan.db"
    try:
        import sys as _sys
        if "/app/tagaudit" not in _sys.path:
            _sys.path.insert(0, "/app/tagaudit")
        from core import db as _db
        cand = getattr(_db, "DB_PATH", None)
        if cand:
            p = str(cand)
    except Exception:
        pass
    return p


def run_selfcheck():
    checks = []

    # 1 - backend + version + etat
    try:
        from config import get_state
        try:
            from config import APP_VERSION
        except Exception:
            APP_VERSION = "?"
        st = get_state()
        checks.append(_c("backend", "Backend + version", "ok",
                         "v%s, etat=%s" % (APP_VERSION, st.get("app_state"))))
    except Exception as e:
        checks.append(_c("backend", "Backend + version", "fail", str(e)))

    # 2 - montage pCloud (lecture seule, comme /api/health)
    try:
        import rclone
        from mountcheck import precheck_target
        err = precheck_target(rclone.PCLOUD_MOUNT)
        if err:
            checks.append(_c("pcloud", "Montage pCloud", "fail", str(err)))
        else:
            checks.append(_c("pcloud", "Montage pCloud", "ok", rclone.PCLOUD_MOUNT))
    except Exception as e:
        checks.append(_c("pcloud", "Montage pCloud", "fail", str(e)))

    # 3 - rclone rc joignable (valide RCLONE_RC_PASS)
    try:
        import rclone
        info = rclone.rc_ping()
        checks.append(_c("rclone", "rclone rc (RCLONE_RC_PASS)", "ok",
                         "rclone %s" % info.get("version", "?")))
    except Exception as e:
        checks.append(_c("rclone", "rclone rc (RCLONE_RC_PASS)", "fail", str(e)))

    # 4 - SMART disques (F19-tuning : warn seulement a old >=5 ans ou SMART_KO ; 3-5 ans = info surveillance)
    try:
        import smartinfo
        from config import SMART_WATCH_YEARS, SMART_OLD_YEARS  # DRY F19
        worst = "ok"
        details = []
        for d in smartinfo.get_all_smart():
            if not d.get("ok"):
                continue
            name = d.get("device", "?")
            poh = d.get("power_on_hours") or 0
            yrs = poh / 8760.0  # heures/an unifiees (365j, cf smartinfo.py)
            smart_ok = d.get("smart_status")
            tag = "ok"
            if smart_ok is False:
                tag = "fail"
            elif yrs >= SMART_OLD_YEARS:  # DRY F19
                tag = "warn"
            if _ORDER[tag] > _ORDER[worst]:
                worst = tag
            details.append("%s %.1fan%s%s%s" % (
                name, yrs, "s" if yrs >= 2 else "",
                " surveillance" if (smart_ok is not False and SMART_WATCH_YEARS <= yrs < SMART_OLD_YEARS) else "",  # DRY F19
                " SMART_KO" if smart_ok is False else ""))
        checks.append(_c("smart", "Disques SMART", worst,
                         ", ".join(details) or "aucun disque"))
    except Exception as e:
        checks.append(_c("smart", "Disques SMART", "warn", str(e)))

    # 5 - fichiers cles + secret rc
    try:
        from config import APP_DATA_ROOT
        cand = {
            "config.json":     Path(APP_DATA_ROOT) / "config.json",
            "rclone.conf":     Path("/config/rclone/rclone.conf"),
            "master_scan.db": Path(_master_db_for_selfcheck()),
        }
        missing = [n for n, p in cand.items() if not p.exists()]
        if not os.environ.get("RCLONE_RC_PASS"):
            missing.append("RCLONE_RC_PASS(env)")
        if missing:
            checks.append(_c("files", "Fichiers/secret cles", "warn",
                             "absent: " + ", ".join(missing)))
        else:
            checks.append(_c("files", "Fichiers/secret cles", "ok", "tous presents"))
    except Exception as e:
        checks.append(_c("files", "Fichiers/secret cles", "fail", str(e)))

    # 6 - audits <-> methodes (port Python du bugcheck #5)
    try:
        ae = Path("/app/tagaudit/audit/audit_engine.py").read_text(encoding="utf-8")
        refs = set(re.findall(r"self\.(_audit_[a-z0-9_]+)", ae))
        defs = set(re.findall(r"def\s+(_audit_[a-z0-9_]+)", ae))
        ghosts = sorted(refs - defs)
        if ghosts:
            checks.append(_c("audits", "Audits <-> methodes", "fail",
                             "fantomes: " + ", ".join(ghosts)))
        else:
            checks.append(_c("audits", "Audits <-> methodes", "ok",
                             "%d audits, tous definis" % len(refs)))
    except Exception as e:
        checks.append(_c("audits", "Audits <-> methodes", "warn", str(e)))

    # 7 - imports tagaudit (try import, meme sys.path que l'app)
    try:
        for p in ("/app", "/app/tagaudit"):
            if p not in sys.path:
                sys.path.insert(0, p)
        mods = []
        for sub in ("core", "providers", "audit", "engine", "export"):
            d = "/app/tagaudit/" + sub
            if os.path.isdir(d):
                for f in os.listdir(d):
                    if f.endswith(".py") and f != "__init__.py":
                        mods.append(sub + "." + f[:-3])
        broken = []
        for m in mods:
            try:
                importlib.import_module(m)
            except Exception as e:
                broken.append("%s:%s" % (m, type(e).__name__))
        if broken:
            checks.append(_c("imports", "Imports tagaudit", "fail", "; ".join(broken)))
        else:
            checks.append(_c("imports", "Imports tagaudit", "ok",
                             "%d modules importes" % len(mods)))
    except Exception as e:
        checks.append(_c("imports", "Imports tagaudit", "warn", str(e)))

    # 8 - compileall /app (syntaxe de tout le backend)
    try:
        ok = compileall.compile_dir("/app", quiet=1, maxlevels=20)
        checks.append(_c("compile", "Compilation /app",
                         "ok" if ok else "fail",
                         "syntaxe OK" if ok else "erreur de compilation"))
    except Exception as e:
        checks.append(_c("compile", "Compilation /app", "warn", str(e)))

    verdict = "ok"
    for c in checks:
        if _ORDER[c["status"]] > _ORDER[verdict]:
            verdict = c["status"]
    return {"checks": checks, "verdict": verdict}
