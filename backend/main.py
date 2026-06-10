"""ZimaCompare v3.8 - Backend FastAPI."""
import asyncio
import importlib.metadata
import json
import os
import platform
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import requests
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

from cleaner import (
    load_plan as load_clean_plan, start_execute as cleaner_start_execute,
    start_scan_db, stop_cleanup,
)
from comparators import hash_cache_clear, hash_cache_load, hash_cache_save, hash_cache_stats
from config import (
    APP_DATA_ROOT, REPORTS_DIR, VALID_PREFIXES, AppConfig, AppState,
    LOG_FILE, ensure_dirs, get_state, load_persisted_state,
    log_buffer, _log_lock, setup_logging, update_state,
    validate_path, path_exists, load_paths_history, disk_info,
    load_profiles, save_profile, delete_profile,
    # v3.12 — .zimaignore
    read_ignore_text, save_ignore_text, parse_ignore_lines,
    compile_ignore_spec, ignore_match, DEFAULT_IGNORE, ZIMAIGNORE_FILE,
    # v3.13 — liste des fichiers ignorés
    IGNORED_FILES_JSON,
)
from installer import build_installer_zip, list_installers
from scanner import (compute_scan_stats, load_scan_results, start_scan, stop_scan,
                     diff_report, diff_report_csv,
                     start_targeted_check, load_targeted_report, TARGETED_CSV,
                     repair_playlist)
import smartinfo
from syncer import start_sync, stop_sync
from tagscan import start_tag_scan, stop_tag_scan, tag_result_info, TAG_SOURCE_DEFAULT, build_tag_export, dirs_payload
import rclone  # pilotage rclone sync via l'API rc du conteneur rclone
from setup import (
    router as setup_router,
    setup_needed,
)

logger = setup_logging()

app = FastAPI(title="ZimaCompare v3", version="3.8.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.include_router(setup_router)

_cfg = AppConfig.load()


@app.on_event("startup")
def _startup():
    ensure_dirs()
    load_persisted_state()
    hash_cache_load()
    logger.info(f"ZimaCompare v3.8 démarré — cache hash : {hash_cache_stats()['entries']} entrées")



class ScanRequest(BaseModel):
    source: str; target: str; method: str = "fast"; filter: Optional[str] = None

class SyncRequest(BaseModel):
    dry_run: bool = True

class ConfigUpdate(BaseModel):
    comparison_method: Optional[str] = None
    verify_after_copy: Optional[bool] = None
    dry_run:           Optional[bool] = None
    max_copy_workers:  Optional[int]  = None
    mirror_deletes:    Optional[bool] = None
    chunk_size_mb:     Optional[int]  = None
    auto_verify_sync:  Optional[bool] = None


def _paths_overlap(source, target):
    s = os.path.realpath(source).rstrip("/") + "/"
    t = os.path.realpath(target).rstrip("/") + "/"
    if s == t: return "Source et cible sont le même dossier"
    if t.startswith(s): return "La cible est à l'intérieur de la source — interdit."
    if s.startswith(t): return "La source est à l'intérieur de la cible — interdit."
    return None


@app.get("/api/status")
def api_status():
    state = get_state()
    state["setup_needed"] = setup_needed()
    return state

@app.get("/api/discover")
def api_discover():
    """v3.11 : ajoute `disk_info` (mapping path → infos d'espace libre).
    Le format des listes `disks` et `network` reste inchangé (strings)
    pour préserver la compatibilité ascendante avec l'ancienne UI."""
    found = {"disks": [], "network": [], "disk_info": {}}
    for root, key in (("/disks", "disks"), ("/network", "network")):
        p = Path(root)
        if not p.exists(): continue
        for child in sorted(p.iterdir()):
            if child.is_dir():
                cp = str(child)
                found[key].append(cp)
                found["disk_info"][cp] = disk_info(cp)
    return found

@app.get("/api/validate-path")
def api_validate_path(path: str = Query(..., min_length=1)):
    return path_exists(path)

@app.get("/api/paths-history")
def api_paths_history():
    history = load_paths_history()
    for entry in history:
        entry["source_status"] = path_exists(entry.get("source", ""))
        entry["target_status"] = path_exists(entry.get("target", ""))
    return history


# -- F8 -- Profils de synchro enregistres --
class ProfileRequest(BaseModel):
    name:   str
    source: str
    target: str
    method: str = "fast"


@app.get("/api/profiles")
def api_profiles_list():
    return load_profiles()


@app.post("/api/profiles")
def api_profiles_save(req: ProfileRequest):
    name = (req.name or "").strip()
    if not name:
        raise HTTPException(400, "Nom de profil requis")
    if not req.source or not req.target:
        raise HTTPException(400, "Source et cible requises")
    return save_profile(name, req.source, req.target, req.method)


@app.delete("/api/profiles/{name}")
def api_profiles_delete(name: str):
    return delete_profile(name)

@app.get("/api/scan-stats")
def api_scan_stats():
    state = get_state()
    if not state.get("scan_done"):
        raise HTTPException(400, "Aucun scan terminé disponible")
    return compute_scan_stats()

@app.get("/api/cache-stats")
def api_cache_stats(): return hash_cache_stats()

@app.post("/api/cache-clear")
def api_cache_clear():
    hash_cache_clear(); hash_cache_save()
    logger.warning("[CACHE] Vidé par l'utilisateur")
    return {"status": "cleared"}

@app.post("/api/scan")
def api_scan(req: ScanRequest):
    if not validate_path(req.source):
        raise HTTPException(400, f"Source invalide — préfixes : {VALID_PREFIXES}")
    if not validate_path(req.target):
        raise HTTPException(400, f"Cible invalide — préfixes : {VALID_PREFIXES}")
    src_state = path_exists(req.source); tgt_state = path_exists(req.target)
    if not src_state["exists"] or not src_state["is_dir"]:
        raise HTTPException(400, f"Source introuvable ou non-dossier : {req.source}")
    if not tgt_state["exists"] or not tgt_state["is_dir"]:
        raise HTTPException(400, f"Cible introuvable ou non-dossier : {req.target}")
    overlap_err = _paths_overlap(req.source, req.target)
    if overlap_err: raise HTTPException(400, overlap_err)
    if rclone.is_rclone_busy():
        raise HTTPException(409, "Une synchro rclone (Cloud) est en cours — "
                                 "attendez sa fin ou arrêtez-la.")
    if not start_scan(req.source, req.target, req.method, _cfg.chunk_size_mb, name_filter=req.filter or ""):
        raise HTTPException(409, "Une opération est déjà en cours")
    return {"status": "started"}

@app.post("/api/sync")
def api_sync(req: SyncRequest):
    state = get_state()
    if state["app_state"] not in (AppState.IDLE, AppState.ERROR):
        raise HTTPException(409, "Une opération est déjà en cours")
    source, target = state["source"], state["target"]
    if not source or not target: raise HTTPException(400, "Lancez d'abord un scan")
    if not state.get("scan_done"): raise HTTPException(400, "Aucun résultat de scan valide")
    overlap_err = _paths_overlap(source, target)
    if overlap_err: raise HTTPException(400, overlap_err)
    if rclone.is_rclone_busy():
        raise HTTPException(409, "Une synchro rclone (Cloud) est en cours — "
                                 "attendez sa fin ou arrêtez-la.")
    if not start_sync(source=source, target=target, dry_run=req.dry_run,
                      verify=_cfg.verify_after_copy, mirror_deletes=_cfg.mirror_deletes,
                      max_workers=_cfg.max_copy_workers, auto_verify=_cfg.auto_verify_sync):
        raise HTTPException(409, "Une opération est déjà en cours")
    return {"status": "started", "dry_run": req.dry_run}

@app.post("/api/abort")
def api_abort():
    state = get_state()
    if state["app_state"] in (AppState.SCANNING, AppState.COMPARING, AppState.VERIFYING):
        stop_scan(); stop_tag_scan(); stop_cleanup()
    elif state["app_state"] == AppState.SYNCING:
        stop_sync(); stop_cleanup()
    else:
        raise HTTPException(400, "Aucune opération en cours")
    return {"status": "aborting"}

@app.post("/api/reset")
def api_reset():
    update_state(app_state=AppState.IDLE, progress=0, total=0, processed=0,
                 current_file="", fps=0, eta_seconds=0, error="",
                 new_count=0, different_count=0, deleted_count=0, identical_count=0,
                 sync_done=0, sync_errors=0, sync_simulated=0, bytes_to_copy=0,
                 scan_done=False, source_changed=False, source_warning="",
                 sync_verified="", sync_verified_msg="")
    return {"status": "ok"}

@app.get("/api/config")
def api_get_config(): return _cfg.__dict__

@app.post("/api/config")
def api_set_config(body: ConfigUpdate):
    global _cfg
    for k, v in body.dict(exclude_none=True).items():
        if hasattr(_cfg, k): setattr(_cfg, k, v)
    _cfg.save()
    return _cfg.__dict__

@app.get("/api/reports")
def api_reports():
    if not REPORTS_DIR.exists(): return []
    reports = []
    for f in sorted(REPORTS_DIR.iterdir(), reverse=True):
        if f.suffix in (".json", ".txt"):
            reports.append({"name": f.name, "size": f.stat().st_size,
                             "date": datetime.fromtimestamp(f.stat().st_mtime).isoformat()})
    return reports

@app.get("/api/reports/{name}")
def api_download_report(name: str):
    path = REPORTS_DIR / name
    if not path.exists() or not path.is_file():
        raise HTTPException(404, "Rapport introuvable")
    return FileResponse(path, filename=name)

@app.get("/api/scan-results")
def api_scan_results(status: Optional[str] = None, limit: int = 500, offset: int = 0):
    results = load_scan_results()
    if status: results = [r for r in results if r["status"] == status]
    return {"total": len(results), "items": results[offset: offset + limit]}


@app.get("/api/diff-report")
def api_diff_report():
    """Rapport des fichiers 'different' du dernier scan, classés par type
    d'écart (taille / lecture impossible / contenu divergent)."""
    return diff_report()


@app.get("/api/diff-report.csv")
def api_diff_report_csv():
    """Export CSV du rapport des fichiers différents (téléchargement)."""
    from fastapi.responses import Response
    csv_text = diff_report_csv()
    return Response(
        content=csv_text,
        media_type="text/csv",
        headers={"Content-Disposition":
                 'attachment; filename="zimacompare-fichiers-differents.csv"'},
    )


class TargetedCheckRequest(BaseModel):
    source: str
    target: str


@app.post("/api/targeted-check")
def api_targeted_check(req: TargetedCheckRequest):
    """Lance le contrôle ciblé niveau 3 : empreinte complète sur les seuls
    fichiers 'different' du dernier scan, avec tentatives multiples de lecture.
    Réutilise l'état de scan global."""
    if not validate_path(req.source) or not validate_path(req.target):
        raise HTTPException(400, f"Chemin invalide — préfixes : {VALID_PREFIXES}")
    ok = start_targeted_check(req.source, req.target)
    if not ok:
        raise HTTPException(409, "Une opération est déjà en cours "
                                 "(scan, synchro ou contrôle).")
    return {"ok": True, "status": "started"}


@app.get("/api/targeted-report")
def api_targeted_report():
    """Dernier rapport de contrôle ciblé : verdict par fichier (identique /
    différent / illisible), classé."""
    return load_targeted_report()


@app.get("/api/targeted-report.csv")
def api_targeted_report_csv():
    """Téléchargement du rapport de contrôle ciblé (CSV séparé)."""
    from fastapi.responses import FileResponse
    if not TARGETED_CSV.exists():
        raise HTTPException(404, "Aucun rapport de contrôle ciblé disponible.")
    return FileResponse(
        str(TARGETED_CSV), media_type="text/csv",
        filename="zimacompare-controle-cible.csv",
    )


@app.get("/api/ignored-files")
def api_ignored_files(limit: int = 500, offset: int = 0):
    """v3.13 — Liste des fichiers/dossiers écartés par .zimaignore lors du
    dernier scan. Alimenté par scanner._write_ignored_files()."""
    if not IGNORED_FILES_JSON.exists():
        return {"total": 0, "listed": 0, "capped": False, "items": [],
                "generated_at": None}
    try:
        with open(IGNORED_FILES_JSON, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except Exception as e:
        raise HTTPException(500, f"Lecture de la liste impossible : {e}")
    items = payload.get("items", [])
    return {
        "total":        payload.get("total", len(items)),
        "listed":       payload.get("listed", len(items)),
        "capped":       payload.get("capped", False),
        "cap":          payload.get("cap"),
        "generated_at": payload.get("generated_at"),
        "items":        items[offset: offset + limit],
    }

@app.get("/api/logs/recent")
def api_logs_recent(n: int = 200):
    with _log_lock: return {"lines": list(log_buffer[-n:])}

@app.websocket("/ws/logs")
async def ws_logs(ws: WebSocket):
    await ws.accept()
    with _log_lock: snapshot = list(log_buffer)
    for line in snapshot: await ws.send_text(line)
    last_idx = len(snapshot)
    try:
        while True:
            await asyncio.sleep(0.4)
            with _log_lock: current = list(log_buffer)
            if len(current) > last_idx:
                for line in current[last_idx:]: await ws.send_text(line)
                last_idx = len(current)
    except WebSocketDisconnect: pass


# ── Dépendances ────────────────────────────────────────────────────────
_TRACKED_PACKAGES = ["python", "fastapi", "uvicorn", "xxhash", "psutil", "pandas", "numpy", "mutagen", "xlsxwriter", "requests", "aiofiles"]


def _pkg_version(pkg):
    if pkg == "python":
        return sys.version.split()[0]
    try:
        return importlib.metadata.version(pkg)
    except importlib.metadata.PackageNotFoundError:
        return "N/A"


def _semver_bump_type(installed, latest):
    if not installed or not latest or "N/A" in (installed, latest): return None
    def parse(v):
        v = v.lstrip("v").split("-")[0].split("+")[0]
        parts = v.split(".")
        return [int(p) if p.isdigit() else 0 for p in parts[:3]] + [0] * (3 - len(parts))
    try:
        i = parse(installed.lstrip("~^")); l = parse(latest)
        if l[0] > i[0]: return "major"
        if l[1] > i[1]: return "minor"
        if l[2] > i[2]: return "patch"
        return None
    except Exception: return None


@app.get("/api/dependencies")
def api_dependencies():
    result = []
    for pkg in _TRACKED_PACKAGES:
        version = _pkg_version(pkg)
        result.append({"name": pkg, "installed": version, "latest": None,
                       "up_to_date": None, "bump": None,
                       "installed_date": None, "latest_date": None})
    return result


@app.get("/api/check-updates")
def api_check_updates():
    result = []
    for pkg in _TRACKED_PACKAGES:
        installed = _pkg_version(pkg)
        entry = {"name": pkg, "installed": installed, "latest": "N/A",
                 "up_to_date": None, "bump": None,
                 "installed_date": None, "latest_date": None}
        if installed == "N/A":
            result.append(entry); continue
        if pkg == "python":
            entry["latest"] = installed; entry["up_to_date"] = True
            result.append(entry); continue
        try:
            r = requests.get(f"https://pypi.org/pypi/{pkg}/json", timeout=5)
            if r.ok:
                info = r.json(); latest = info["info"]["version"]
                entry["latest"]     = latest
                entry["up_to_date"] = (installed == latest)
                entry["bump"]       = _semver_bump_type(installed, latest)
                releases = info.get("releases", {})
                if latest in releases and releases[latest]:
                    entry["latest_date"] = releases[latest][0].get("upload_time", "")[:10] or None
                if installed in releases and releases[installed]:
                    entry["installed_date"] = releases[installed][0].get("upload_time", "")[:10] or None
        except Exception as e:
            logger.warning(f"[DEPS] erreur récup {pkg}: {e}")
        result.append(entry)
    return result


@app.get("/api/npm-info")
def api_npm_info(package: str = Query(..., min_length=1, max_length=214),
                 installed: Optional[str] = Query(None)):
    safe = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-_/@")
    if not package or any(c not in safe for c in package):
        raise HTTPException(400, "Nom de paquet npm invalide")
    out = {"name": package, "latest": "N/A", "latest_date": None,
           "installed_date": None, "bump": None}
    try:
        r = requests.get(f"https://registry.npmjs.org/{package}", timeout=5)
        if not r.ok: return out
        data = r.json()
        latest = data.get("dist-tags", {}).get("latest", "N/A")
        times  = data.get("time", {})
        out["latest"]      = latest
        out["latest_date"] = (times.get(latest, "") or "")[:10] or None
        if installed:
            clean_inst = installed.lstrip("~^")
            out["installed_date"] = (times.get(clean_inst, "") or "")[:10] or None
            out["bump"]           = _semver_bump_type(installed, latest)
    except Exception as e:
        logger.warning(f"[NPM] erreur récup {package}: {e}")
    return out


class NpmAuditRequest(BaseModel):
    deps: dict


@app.post("/api/npm-audit")
def api_npm_audit(body: NpmAuditRequest):
    if not body.deps or not isinstance(body.deps, dict):
        raise HTTPException(400, "Le champ 'deps' doit être un dict {nom: version}")
    payload = {name: [version.lstrip("~^")] for name, version in body.deps.items()
               if isinstance(version, str)}
    try:
        r = requests.post(
            "https://registry.npmjs.org/-/npm/v1/security/advisories/bulk",
            json=payload, timeout=10,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        if not r.ok:
            return {"status": "error", "http_status": r.status_code, "advisories": {}}
        data = r.json()
        simplified = {}; total_count = 0
        for name, advisories in data.items():
            simplified[name] = [
                {"id": a.get("id"), "title": a.get("title", "Sans titre"),
                 "severity": a.get("severity", "unknown"),
                 "vulnerable": a.get("vulnerable_versions", ""),
                 "url": a.get("url", "")} for a in advisories
            ]
            total_count += len(simplified[name])
        return {"status": "ok", "total": total_count, "advisories": simplified}
    except Exception as e:
        return {"status": "error", "error": str(e), "advisories": {}}


# ── Installer ────────────────────────────────────────────────────────────
class InstallerRequest(BaseModel):
    include_paths_history: bool = True


@app.get("/api/installers")
def api_installers(): return list_installers()


@app.post("/api/installers/build")
def api_installer_build(body: InstallerRequest = InstallerRequest()):
    # Recherche du docker-compose réel, par ordre de préférence. Le compose
    # qui tourne vraiment est celui géré par CasaOS — on le cherche en premier.
    candidates = [
        Path("/var/lib/casaos/apps/zimacompare/docker-compose.yml"),
        Path("/var/lib/casaos/apps/zimacompare/docker-compose.yaml"),
        Path("/DATA/AppData/zimacompare-v3/docker-compose.yaml"),
        Path("/app/../docker-compose.yaml"),
    ]
    compose = next((c.resolve() for c in candidates if c.exists()), None)
    out_path = build_installer_zip(
        include_paths_history=body.include_paths_history,
        docker_compose_path=compose,
    )
    return {"name": out_path.name, "size": out_path.stat().st_size,
            "path": str(out_path), "url": f"/api/installers/{out_path.name}"}


@app.get("/api/installers/{name}")
def api_installer_download(name: str):
    if not name.startswith("zimacompare-installer-") or not name.endswith(".zip"):
        raise HTTPException(400, "Nom de fichier invalide")
    if "/" in name or "\\" in name or ".." in name:
        raise HTTPException(400, "Nom de fichier invalide")
    path = APP_DATA_ROOT / name
    if not path.exists() or not path.is_file():
        raise HTTPException(404, "Installer introuvable")
    return FileResponse(path, filename=name, media_type="application/zip")


@app.delete("/api/installers/{name}")
def api_installer_delete(name: str):
    if not name.startswith("zimacompare-installer-") or not name.endswith(".zip"):
        raise HTTPException(400, "Nom de fichier invalide")
    if "/" in name or "\\" in name or ".." in name:
        raise HTTPException(400, "Nom de fichier invalide")
    path = APP_DATA_ROOT / name
    if not path.exists(): raise HTTPException(404, "Installer introuvable")
    path.unlink()
    return {"status": "deleted", "name": name}


# ── NEW v3.8 — SMART ─────────────────────────────────────────────────────
@app.get("/api/smart/devices")
def api_smart_devices():
    """Liste les devices détectés et leurs infos SMART (avec cache 60s)."""
    return smartinfo.get_all_smart()


@app.get("/api/smart/device/{device_name}")
def api_smart_device(device_name: str):
    """Détail d'un device. `device_name` est le suffixe (ex: 'sda'), on
    reconstitue /dev/<device_name> avec validation stricte."""
    # Sécurité : pattern strict pour éviter toute injection
    if not device_name.replace("nvme", "").replace("sd", "").replace("hd", "") \
            .replace("p", "").isalnum() if device_name else True:
        if not all(c.isalnum() for c in device_name):
            raise HTTPException(400, "Nom de device invalide")
    if "/" in device_name or ".." in device_name:
        raise HTTPException(400, "Nom de device invalide")
    device = f"/dev/{device_name}"
    info = smartinfo.get_device_smart(device, use_cache=False)
    if not info.get("ok"):
        raise HTTPException(404, f"Pas d'info SMART pour {device}")
    return info


@app.post("/api/smart/refresh")
def api_smart_refresh():
    smartinfo.clear_cache()
    return {"status": "cleared"}


# ── NEW v3.8 — Nettoyage .db ─────────────────────────────────────────────
class CleanScanRequest(BaseModel):
    root:  str
    force: bool = False  # v3.10 : ignorer la protection audio


class CleanExecRequest(BaseModel):
    root:    str
    dry_run: bool = True


@app.post("/api/clean/scan")
def api_clean_scan(req: CleanScanRequest):
    if not validate_path(req.root):
        raise HTTPException(400, f"Chemin invalide — préfixes : {VALID_PREFIXES}")
    if not path_exists(req.root)["is_dir"]:
        raise HTTPException(400, f"Dossier introuvable : {req.root}")
    if not start_scan_db(req.root, force=req.force):
        raise HTTPException(409, "Une opération est déjà en cours")
    return {"status": "started", "root": req.root, "force": req.force}


@app.get("/api/clean/plan")
def api_clean_plan():
    plan = load_clean_plan()
    if not plan:
        raise HTTPException(404, "Aucun plan disponible — lancez d'abord un scan")
    return plan


@app.post("/api/clean/execute")
def api_clean_execute(req: CleanExecRequest):
    if not validate_path(req.root):
        raise HTTPException(400, f"Chemin invalide — préfixes : {VALID_PREFIXES}")
    plan = load_clean_plan()
    if not plan:
        raise HTTPException(400, "Aucun plan disponible — relancez un scan")
    if plan["root"] != req.root:
        raise HTTPException(400, f"Le plan a été généré pour {plan['root']!r}, "
                                  f"pas pour {req.root!r}. Relance un scan.")
    if not cleaner_start_execute(req.root, req.dry_run):
        raise HTTPException(409, "Une opération est déjà en cours")
    return {"status": "started", "dry_run": req.dry_run}


# ── v3.12 — Gestion du .zimaignore (gitignore-style) ─────────────────
class ZimaignoreUpdateRequest(BaseModel):
    content: str


class ZimaignoreTestRequest(BaseModel):
    root:    str
    content: Optional[str] = None      # si fourni, utilise ce contenu ; sinon, fichier courant
    max_samples: int       = 50


def _zimaignore_payload() -> dict:
    text = read_ignore_text()
    patterns = parse_ignore_lines(text)
    try:
        mtime = ZIMAIGNORE_FILE.stat().st_mtime if ZIMAIGNORE_FILE.exists() else None
    except Exception:
        mtime = None
    return {
        "content":         text,
        "patterns_active": len(patterns),
        "patterns":        patterns,
        "bytes":           len(text.encode("utf-8")),
        "max_bytes":       64 * 1024,
        "last_modified":   mtime,
        "defaults":        list(DEFAULT_IGNORE),
    }


@app.get("/api/zimaignore")
def api_zimaignore_get():
    return _zimaignore_payload()


@app.put("/api/zimaignore")
def api_zimaignore_put(req: ZimaignoreUpdateRequest):
    result = save_ignore_text(req.content)
    if not result["ok"]:
        raise HTTPException(400, "; ".join(result["errors"]) or "Erreur inconnue")
    return _zimaignore_payload()


@app.post("/api/zimaignore/reset")
def api_zimaignore_reset():
    """Restaure le fichier .zimaignore à ses défauts (DEFAULT_IGNORE)."""
    header = (
        "# ZimaCompare .zimaignore — patterns gitignore-style\n"
        "# Patterns par défaut restaurés.\n"
        "#\n"
    )
    body = "\n".join(DEFAULT_IGNORE) + "\n"
    result = save_ignore_text(header + body)
    if not result["ok"]:
        raise HTTPException(500, "; ".join(result["errors"]))
    return _zimaignore_payload()


@app.post("/api/zimaignore/test")
def api_zimaignore_test(req: ZimaignoreTestRequest):
    """Simule l'application des patterns sur un dossier réel.

    Parcourt jusqu'à 50 000 entrées pour ne pas figer le serveur, retourne :
      - ignored_count : combien d'entrées seraient ignorées
      - kept_count    : combien resteraient
      - samples       : N premiers chemins ignorés (par défaut 50)
      - truncated     : True si on a stoppé avant la fin
    """
    if not validate_path(req.root):
        raise HTTPException(400, f"Chemin invalide — préfixes : {VALID_PREFIXES}")
    pp = Path(req.root)
    if not pp.exists() or not pp.is_dir():
        raise HTTPException(400, f"Dossier introuvable : {req.root}")

    # On utilise soit le contenu fourni (preview), soit le fichier en place.
    if req.content is not None:
        patterns = parse_ignore_lines(req.content)
        spec = compile_ignore_spec(patterns)
    else:
        spec = compile_ignore_spec()

    SCAN_LIMIT = 50_000
    base_str = str(pp)
    base_len = len(base_str) + 1
    ignored = kept = 0
    samples: List[str] = []
    truncated = False

    for current_root, dirs, files in os.walk(pp):
        if ignored + kept >= SCAN_LIMIT:
            truncated = True
            break
        # Filtrer les dossiers in-place pour ne pas descendre dans les ignorés
        kept_dirs = []
        for d in dirs:
            full = os.path.join(current_root, d)
            rel = full[base_len:] if len(full) > base_len else d
            if ignore_match(spec, rel, True):
                ignored += 1
                if len(samples) < max(0, min(req.max_samples, 200)):
                    samples.append(rel + "/")
            else:
                kept_dirs.append(d)
        dirs[:] = kept_dirs
        for f in files:
            full = os.path.join(current_root, f)
            rel = full[base_len:] if len(full) > base_len else f
            if ignore_match(spec, rel, False):
                ignored += 1
                if len(samples) < max(0, min(req.max_samples, 200)):
                    samples.append(rel)
            else:
                kept += 1

    return {
        "ignored_count": ignored,
        "kept_count":    kept,
        "samples":       samples,
        "truncated":     truncated,
        "scan_limit":    SCAN_LIMIT,
    }


# ── Export contexte ──────────────────────────────────────────────────────
def _file_inventory(directory: Path, max_entries: int = 200) -> dict:
    out = {}
    if not directory.exists() or not directory.is_dir(): return out
    count = 0
    try:
        for root, dirs, files in os.walk(directory):
            for fname in files:
                if count >= max_entries: return out
                p = Path(root) / fname
                try:
                    st = p.stat()
                    rel = str(p.relative_to(directory))
                    out[rel] = {"size": st.st_size,
                                "mtime": datetime.fromtimestamp(st.st_mtime).isoformat(timespec='seconds')}
                except Exception: pass
                count += 1
    except Exception: pass
    return out


@app.get("/api/export-context")
def api_export_context():
    py_deps = {}
    for pkg in _TRACKED_PACKAGES:
        py_deps[pkg] = _pkg_version(pkg)
    npm_deps_decl = {}
    npm_pkg = Path("/app_frontend/package.json")
    if npm_pkg.exists():
        try:
            data = json.loads(npm_pkg.read_text())
            npm_deps_decl = {"prod": data.get("dependencies", {}),
                              "dev":  data.get("devDependencies", {}),
                              "version": data.get("version", "?")}
        except Exception: pass
    backend_inventory = _file_inventory(Path("/app"), max_entries=50)
    history = load_paths_history()
    data_files = {}
    if APP_DATA_ROOT.exists():
        for f in APP_DATA_ROOT.iterdir():
            if f.is_file():
                try:
                    st = f.stat()
                    data_files[f.name] = {"size": st.st_size,
                                           "mtime": datetime.fromtimestamp(st.st_mtime).isoformat(timespec='seconds')}
                except Exception: pass
    with _log_lock: recent_logs = list(log_buffer[-100:])

    # NEW v3.8 : on inclut un résumé SMART (sans tout le détail des attributs)
    smart_summary = []
    try:
        for d in smartinfo.get_all_smart():
            if d.get("ok"):
                smart_summary.append({
                    "device":           d.get("device"),
                    "model":            d.get("model"),
                    "disk_type":        d.get("disk_type"),
                    "capacity_bytes":   d.get("capacity_bytes"),
                    "smart_status":     d.get("smart_status"),
                    "temperature":      d.get("temperature"),
                    "power_on_hours":   d.get("power_on_hours"),
                })
    except Exception as e:
        logger.warning(f"[CONTEXT] smart summary failed: {e}")

    return {
        "schema_version": "1.1",
        "exported_at":    datetime.now().isoformat(timespec='seconds'),
        "app_version":    app.version,
        "system": {
            "python_version": sys.version.split()[0],
            "platform":       platform.platform(),
            "container":      True,
        },
        "python_deps":   py_deps,
        "npm_declared":  npm_deps_decl,
        "config":        _cfg.__dict__,
        "state":         get_state(),
        "cache":         hash_cache_stats(),
        "paths_history": history,
        "data_files":    data_files,
        "backend_files": list(backend_inventory.keys()),
        "installers":    list_installers(),
        "recent_logs":   recent_logs,
        "smart":         smart_summary,
        "notes_for_assistant": (
            "État complet d'une instance ZimaCompare. Pour reprendre la conversation, "
            "demander à l'utilisateur le ZIP installer le plus récent (référencé dans 'installers')."
        ),
    }


# ══════════════════════════════════════════════════════════════════════════
#  rclone — pilotage de la synchro vers pCloud via l'API rc
#  (voir rclone.py et NOTE-ARCHITECTURE-rclone-sync.md)
# ══════════════════════════════════════════════════════════════════════════
class RcloneSyncRequest(BaseModel):
    source:  str                       # chemin local (préfixe /disks/ ou /network/)
    dest:    str                       # destination rclone (ex: pcloud:00_PcloudMusic)
    dry_run: bool = True               # simulation par défaut
    mirror:  bool = False              # False = copy (sans suppression) ; True = sync (miroir)


@app.get("/api/rclone/status")
def api_rclone_status():
    """État courant de la synchro rclone (interrogé en boucle par l'UI)."""
    return rclone.get_rclone_state()


@app.get("/api/rclone/ping")
def api_rclone_ping():
    """Teste que le démon rclone (conteneur) répond sur son API rc."""
    try:
        info = rclone.rc_ping()
        return {"ok": True, "version": info.get("version", "?"),
                "os": info.get("os", ""), "arch": info.get("arch", "")}
    except rclone.RcloneError as e:
        raise HTTPException(502, str(e))


@app.get("/api/rclone/lsd")
def api_rclone_lsd(path: str = Query("", description="Chemin remote, vide = racine pCloud")):
    """Liste les sous-dossiers d'un chemin du remote pCloud.
    Alimente le menu déroulant de destination de l'onglet Cloud (étape 3)."""
    remote = path.strip() or rclone.RC_REMOTE
    try:
        dirs = rclone.rc_list_dirs(remote)
        return {"ok": True, "path": remote, "dirs": dirs}
    except rclone.RcloneError as e:
        raise HTTPException(502, str(e))


@app.get("/api/rclone/health")
def api_rclone_health():
    """Bilan de santé rclone : démon joignable, montage pCloud sain, quota.
    Ne lève pas — chaque section porte son propre statut ok/ko."""
    return rclone.rclone_health()


@app.post("/api/rclone/sync")
def api_rclone_sync(req: RcloneSyncRequest):
    """Démarre une synchro rclone (transfert DIRECT local → pcloud:,
    sans passer par le montage FUSE)."""
    # Garde-fou croisé : pas de synchro rclone pendant une opération ZimaCompare.
    st = get_state()
    if st["app_state"] in (AppState.SCANNING, AppState.COMPARING,
                            AppState.SYNCING, AppState.VERIFYING):
        raise HTTPException(409, "Une opération ZimaCompare (scan/sync) est en "
                                 "cours — attendez sa fin.")
    # La source doit être un chemin local valide de l'application.
    if not validate_path(req.source):
        raise HTTPException(400, f"Source invalide — préfixes : {VALID_PREFIXES}")
    src_state = path_exists(req.source)
    if not src_state["exists"] or not src_state["is_dir"]:
        raise HTTPException(400, f"Source introuvable ou non-dossier : {req.source}")
    try:
        return rclone.start_rclone_sync(
            req.source, req.dest, dry_run=req.dry_run, mirror=req.mirror,
        )
    except rclone.RcloneError as e:
        # 409 si une synchro tourne déjà, 502 pour les autres erreurs rc.
        msg = str(e)
        code = 409 if "déjà en cours" in msg else 502
        raise HTTPException(code, msg)


@app.post("/api/rclone/abort")
def api_rclone_abort():
    """Arrête la synchro rclone en cours."""
    try:
        return rclone.abort_rclone_sync()
    except rclone.RcloneError as e:
        raise HTTPException(400, str(e))


@app.get("/api/rclone/scan-summary")
def api_rclone_scan_summary():
    """Aperçu du dernier scan pour le mode rapide : couple source/cible,
    nombre de fichiers à transférer, fraîcheur. Ne lance rien."""
    return rclone.scan_summary_for_fast_sync()


class RcloneFastSyncRequest(BaseModel):
    source:  str
    dest:    str
    dry_run: bool = True


@app.post("/api/rclone/sync-from-scan")
def api_rclone_sync_from_scan(req: RcloneFastSyncRequest):
    """Mode rapide : transfère uniquement les fichiers new+different du
    dernier scan ZimaCompare (via --files-from). rclone ne re-compare rien."""
    # Garde-fou croisé : pas de synchro rclone pendant une opération ZimaCompare.
    st = get_state()
    if st["app_state"] in (AppState.SCANNING, AppState.COMPARING,
                            AppState.SYNCING, AppState.VERIFYING):
        raise HTTPException(409, "Une opération ZimaCompare (scan/sync) est en "
                                 "cours — attendez sa fin.")
    if not validate_path(req.source):
        raise HTTPException(400, f"Source invalide — préfixes : {VALID_PREFIXES}")
    src_state = path_exists(req.source)
    if not src_state["exists"] or not src_state["is_dir"]:
        raise HTTPException(400, f"Source introuvable ou non-dossier : {req.source}")
    try:
        return rclone.start_rclone_fast_sync(
            req.source, req.dest, dry_run=req.dry_run,
        )
    except rclone.RcloneError as e:
        msg = str(e)
        code = 409 if "déjà en cours" in msg else 400
        raise HTTPException(code, msg)


# ── T2 — Healthcheck profond (backend + montage pCloud) ──────────────────
PCLOUD_MOUNT = "/network/pCloud"


@app.get("/api/health")
def api_health():
    """Sonde santé pour le healthcheck Docker. Légère (lecture /proc/mounts
    + stat, aucun appel réseau) : confirme que le backend répond ET que le
    montage pCloud est présent (pas retombé sur le disque local du conteneur).
    200 si tout va bien, 503 sinon."""
    from fastapi.responses import JSONResponse
    from mountcheck import precheck_target
    mount_err = precheck_target(PCLOUD_MOUNT)
    if mount_err:
        return JSONResponse(
            status_code=503,
            content={"ok": False, "backend": "ok",
                     "pcloud_mount": "down", "detail": mount_err},
        )
    return {"ok": True, "backend": "ok", "pcloud_mount": "ok"}


# ── F4 — Playlist .m3u8 des albums à réparer (EZ CD) ─────────────────────
@app.get("/api/playlist/repair-preview")
def api_playlist_repair_preview(pc_root: str = Query(""),
                                kinds: str = Query("read_error,content")):
    kt = tuple(k.strip() for k in kinds.split(",") if k.strip())
    rep = repair_playlist(pc_root, kt)
    return {"album_count": rep["album_count"], "track_count": rep["track_count"],
            "albums": rep["albums"][:300], "pc_root": pc_root, "kinds": list(kt)}


@app.get("/api/playlist/repair.m3u8")
def api_playlist_repair_m3u8(pc_root: str = Query(..., min_length=1),
                             kinds: str = Query("read_error,content")):
    from fastapi.responses import Response
    kt = tuple(k.strip() for k in kinds.split(",") if k.strip())
    rep = repair_playlist(pc_root, kt)
    if rep["track_count"] == 0:
        raise HTTPException(404, "Aucun album à réparer dans le dernier scan.")
    data = rep["m3u8"].encode("utf-8-sig")
    return Response(content=data, media_type="audio/x-mpegurl",
                    headers={"Content-Disposition":
                             'attachment; filename="albums-a-reparer.m3u8"'})


# -- F12 -- Inventaire des types de fichiers (par extension, lecture seule) --
_FT_AUDIO = {".mp3", ".flac", ".m4a", ".wav", ".ogg", ".wma", ".aac", ".alac",
             ".aiff", ".aif", ".opus", ".ape", ".dsf"}
_FT_IMAGE = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tiff", ".tif",
             ".heic", ".svg", ".raw"}
_FT_VIDEO = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v",
             ".mpg", ".mpeg", ".ts"}
_FT_DOC = {".pdf", ".doc", ".docx", ".txt", ".md", ".rtf", ".odt", ".xls",
           ".xlsx", ".csv", ".ppt", ".pptx", ".epub"}
_FT_ARCHIVE = {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz"}


def _ft_category(ext: str) -> str:
    if ext in _FT_AUDIO: return "audio"
    if ext in _FT_IMAGE: return "image"
    if ext in _FT_VIDEO: return "video"
    if ext in _FT_DOC: return "doc"
    if ext in _FT_ARCHIVE: return "archive"
    return "autre"


@app.get("/api/file-types")
def api_file_types(path: str = Query(..., min_length=1)):
    """Inventaire des fichiers d'une arborescence, agrege par extension.
    Lecture seule (os.walk + os.stat, metadonnees only)."""
    if not validate_path(path):
        raise HTTPException(400, f"Chemin invalide — prefixes : {VALID_PREFIXES}")
    st = path_exists(path)
    if not st["exists"] or not st["is_dir"]:
        raise HTTPException(404, f"Dossier introuvable : {path}")
    CAP = 1_000_000
    ext_count = {}
    ext_bytes = {}
    total_files = 0
    total_bytes = 0
    truncated = False
    for root, dirs, files in os.walk(path, onerror=lambda e: None):
        for fn in files:
            if total_files >= CAP:
                truncated = True
                break
            ext = os.path.splitext(fn)[1].lower() or "(sans extension)"
            try:
                sz = os.stat(os.path.join(root, fn)).st_size
            except Exception:
                sz = 0
            ext_count[ext] = ext_count.get(ext, 0) + 1
            ext_bytes[ext] = ext_bytes.get(ext, 0) + sz
            total_files += 1
            total_bytes += sz
        if truncated:
            break
    extensions = sorted(
        [{"ext": e, "count": ext_count[e], "bytes": ext_bytes[e],
          "category": _ft_category(e)} for e in ext_count],
        key=lambda x: x["bytes"], reverse=True,
    )
    cat_count = {}
    cat_bytes = {}
    for e in extensions:
        c = e["category"]
        cat_count[c] = cat_count.get(c, 0) + e["count"]
        cat_bytes[c] = cat_bytes.get(c, 0) + e["bytes"]
    categories = sorted(
        [{"category": c, "count": cat_count[c], "bytes": cat_bytes[c]} for c in cat_count],
        key=lambda x: x["bytes"], reverse=True,
    )
    return {"ok": True, "path": path, "total_files": total_files,
            "total_bytes": total_bytes, "ext_count": len(extensions),
            "extensions": extensions, "categories": categories,
            "truncated": truncated}


# -- v9 -- Scan de tags (ZimaTAG integre) ----------------------------------
class TagScanRequest(BaseModel):
    source: Optional[str] = None
    formats: Optional[List[str]] = None
    filter: Optional[str] = None
    limit: Optional[int] = None


@app.post("/api/tag/scan")
def api_tag_scan(req: TagScanRequest = TagScanRequest()):
    source = (req.source or TAG_SOURCE_DEFAULT).strip()
    if not validate_path(source):
        raise HTTPException(400, f"Source invalide -- prefixes : {VALID_PREFIXES}")
    stt = path_exists(source)
    if not stt["exists"] or not stt["is_dir"]:
        raise HTTPException(400, f"Source introuvable ou non-dossier : {source}")
    res = start_tag_scan(source, formats=req.formats, name_filter=req.filter, limit=req.limit)
    if res == "busy":
        raise HTTPException(409, "Une operation est deja en cours")
    if res == "nomatch":
        raise HTTPException(400, "Aucun dossier ne correspond au filtre")
    return {"status": "started", "source": source}


@app.post("/api/tag/abort")
def api_tag_abort():
    if not stop_tag_scan():
        raise HTTPException(400, "Aucun scan-tag en cours")
    return {"status": "aborting"}


@app.get("/api/tag/result")
def api_tag_result():
    return tag_result_info()


@app.get("/api/tag/export.xlsx")
def api_tag_export():
    info = tag_result_info()
    if not info.get("exists") or not info.get("rows"):
        raise HTTPException(400, "Aucun master_scan.csv -- lancez d'abord un scan-tag")
    try:
        path = build_tag_export()
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    except Exception as e:
        raise HTTPException(500, "Export Excel: %s" % e)
    from fastapi.responses import FileResponse
    import os
    return FileResponse(path, filename=os.path.basename(path),
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.get("/api/tag/dirs")
def api_tag_dirs(refresh: int = 0, source: Optional[str] = None):
    """Index des sous-dossiers de la source (comptage par format) pour le
    filtre interactif de l'onglet ZimaTAG. Mis en cache cote backend."""
    return dirs_payload(source, refresh=bool(refresh))


@app.get("/api/tag/progress")
def api_tag_progress():
    from tagscan import tag_progress
    return tag_progress()
