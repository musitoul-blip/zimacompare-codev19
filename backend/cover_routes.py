import os
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import compressor

router = APIRouter(prefix="/api/cover")

COVER_ALLOW_WRITE = os.environ.get("COVER_ALLOW_WRITE", "false").strip().lower() == "true"
ALLOWED_BROWSE_PREFIXES = [
    p.strip() for p in os.environ.get(
        "ALLOWED_BROWSE_PREFIXES", "/disks,/network"
    ).split(",") if p.strip()
]


def _check_allowed_prefix(path_str: str):
    norm = os.path.normpath(path_str)
    for prefix in ALLOWED_BROWSE_PREFIXES:
        prefix_norm = os.path.normpath(prefix)
        if norm == prefix_norm or norm.startswith(prefix_norm + os.sep):
            return
    raise HTTPException(
        400,
        f"Chemin hors des emplacements autorisés ({ALLOWED_BROWSE_PREFIXES}) : {path_str}",
    )


class JobRequest(BaseModel):
    source: str
    max_kb: int = 800
    min_quality: int = 40
    max_quality: int = 95
    force_all: bool = False
    backup: bool = True
    max_px: int = 0
    allow_downscale: bool = False
    only_paths: list = []


@router.get("/ping")
def ping():
    return {"ok": True}


@router.get("/browse")
def cover_browse(path: str = "/disks"):
    """Explorateur en lecture seule, restreint à ALLOWED_BROWSE_PREFIXES."""
    _check_allowed_prefix(path)
    p = Path(path or "/disks")
    if not p.exists() or not p.is_dir():
        raise HTTPException(400, f"Dossier invalide : {path}")

    def _safe_is_dir(entry) -> Optional[bool]:
        try:
            return entry.is_dir(follow_symlinks=False)
        except OSError:
            return None

    try:
        raw = list(os.scandir(p))
    except PermissionError:
        raise HTTPException(403, f"Accès refusé : {path}")

    scored = [(e, _safe_is_dir(e)) for e in raw]
    scored = [(e, d) for e, d in scored if d is not None]
    scored.sort(key=lambda ed: (not ed[1], ed[0].name.lower()))

    entries = []
    for e, is_dir in scored:
        if not is_dir and not e.name.lower().endswith(".csv"):
            continue
        entries.append({"name": e.name, "is_dir": is_dir})

    parent = str(p.parent) if str(p) != str(p.parent) else None
    return {"path": str(p), "parent": parent, "entries": entries}


@router.post("/preview")
def cover_preview(req: JobRequest):
    res = compressor.start_job(
        req.source, req.max_kb, req.min_quality, req.max_quality,
        req.force_all, req.backup, apply_write=False,
        max_px=req.max_px, allow_downscale=req.allow_downscale,
    )
    if res == "busy":
        raise HTTPException(409, "Un job est déjà en cours")
    if res == "notfound":
        raise HTTPException(400, f"Dossier introuvable : {req.source}")
    return {"status": "started", "mode": "preview"}


@router.post("/apply")
def cover_apply(req: JobRequest):
    if not COVER_ALLOW_WRITE:
        raise HTTPException(403, "Écriture pochettes désactivée (COVER_ALLOW_WRITE non activé — LOT 5)")
    res = compressor.start_job(
        req.source, req.max_kb, req.min_quality, req.max_quality,
        req.force_all, req.backup, apply_write=True,
        max_px=req.max_px, allow_downscale=req.allow_downscale,
        only_paths=req.only_paths or None,
    )
    if res == "busy":
        raise HTTPException(409, "Un job est déjà en cours")
    if res == "notfound":
        raise HTTPException(400, f"Dossier introuvable : {req.source}")
    return {"status": "started", "mode": "apply"}


@router.post("/abort")
def cover_abort():
    if not compressor.stop_job():
        raise HTTPException(400, "Aucun job en cours")
    return {"status": "aborting"}


@router.get("/progress")
def cover_progress():
    return compressor.STATE.as_dict()


@router.get("/result")
def cover_result():
    return compressor.result_info()


@router.get("/rows")
def cover_rows(only_needs: bool = False, limit: int = 2000):
    rows = compressor.read_result_rows()
    if only_needs:
        rows = [r for r in rows if r.get("needs_processing") == "True"]
    return {"rows": rows[:limit], "total": len(rows)}


@router.get("/consistency")
def cover_consistency():
    return compressor.consistency_report()
