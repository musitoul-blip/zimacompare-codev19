"""ZimaCompare v18 (AIO) — Module setup wizard.

Refonte pour l'image AIO supervisée par S6-Overlay :
- rclone tourne DANS le même conteneur et monte pCloud sur /network/pCloud.
- Le service S6 `rclone` attend (poll) l'apparition de /config/rclone/rclone.conf,
  puis monte automatiquement. Le backend n'a donc PLUS besoin de :
    * mount --bind / make-shared        (plus de propagation FUSE vers l'hôte)
    * redémarrer rclone via Docker socket (S6 supervise rclone localement)
- La finalisation se résume à : vérifier rclone.conf -> attendre que /network/pCloud
  réponde (via l'API rc locale) -> écrire le sentinel setup_done.

Détection : rclone.conf absent OU fichier setup_done absent.
"""

import logging
import os
import shutil
import time
from pathlib import Path

import requests
from fastapi import APIRouter, HTTPException, UploadFile, File
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

logger = logging.getLogger("zimacompare")

# ---------------------------------------------------------------------------
# Chemins (AIO : rclone.conf vu au MEME chemin /config/rclone par tous les
# process du conteneur - backend et rclone)
# ---------------------------------------------------------------------------
APP_DATA_ROOT   = Path("/app_data")
APP_RCLONE_DIR  = Path(os.environ.get("RCLONE_CONFIG_DIR", "/config/rclone"))
SETUP_DONE_FILE = APP_DATA_ROOT / "setup_done"
RCLONE_CONF     = APP_RCLONE_DIR / "rclone.conf"

# API rc de rclone - locale au conteneur en AIO (etait l'URL du conteneur rclone)
RCLONE_RC_URL   = os.environ.get("RCLONE_RC_URL",  "http://127.0.0.1:5572").rstrip("/")
RCLONE_RC_USER  = os.environ.get("RCLONE_RC_USER", "zima")
RCLONE_RC_PASS  = os.environ.get("RCLONE_RC_PASS", "CHANGE-ME-rclone-2026")

router = APIRouter(prefix="/api/setup", tags=["setup"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def setup_needed() -> bool:
    """Retourne True si le setup wizard doit etre affiche."""
    return not RCLONE_CONF.exists() or not SETUP_DONE_FILE.exists()


def _validate_rclone_conf(path: Path) -> tuple[bool, str]:
    """Valide qu'un fichier rclone.conf contient bien une section pCloud EU."""
    try:
        content = path.read_text(encoding="utf-8")
    except Exception as e:
        return False, f"Impossible de lire le fichier : {e}"
    if "[pcloud]" not in content:
        return False, "Le fichier ne contient pas de section [pcloud]."
    if "eapi.pcloud.com" not in content:
        return False, (
            "Le compte pCloud semble etre sur la region US "
            "(hostname = api.pcloud.com). "
            "ZimaCompare attend un compte europeen (hostname = eapi.pcloud.com)."
        )
    if "token" not in content.lower():
        return False, "Le fichier ne contient pas de token d'acces pCloud."
    return True, "OK"


def _wait_pcloud_mounted(timeout: int = 90) -> tuple[bool, str]:
    """Attend que rclone monte pCloud (polling de l'API rc locale).

    En AIO, le service S6 `rclone` detecte rclone.conf via son propre poll
    (toutes les ~5 s) puis monte /network/pCloud et expose l'API rc. On attend
    donc simplement que /vfs/list reponde 200.
    """
    deadline = time.time() + timeout
    last_err = "pas encore monte"
    while time.time() < deadline:
        try:
            r = requests.post(
                f"{RCLONE_RC_URL}/vfs/list",
                auth=(RCLONE_RC_USER, RCLONE_RC_PASS),
                timeout=5,
            )
            if r.status_code == 200:
                logger.info("[SETUP] pCloud monte et accessible")
                return True, "OK"
            last_err = f"rc a repondu {r.status_code}"
        except Exception as e:
            last_err = str(e)
        time.sleep(3)
    return False, f"pCloud non accessible apres {timeout}s ({last_err})"


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

class RclonePathRequest(BaseModel):
    path: str


@router.get("/status")
def get_setup_status():
    """Retourne si le setup wizard est necessaire."""
    return {
        "setup_needed": setup_needed(),
        "rclone_conf_present": RCLONE_CONF.exists(),
        "setup_done": SETUP_DONE_FILE.exists(),
    }


@router.post("/upload-rclone")
async def upload_rclone_conf(file: UploadFile = File(...)):
    """Recoit le fichier rclone.conf uploade depuis le navigateur."""
    APP_RCLONE_DIR.mkdir(parents=True, exist_ok=True)

    tmp = APP_RCLONE_DIR / "rclone.conf.tmp"
    try:
        content = await file.read()
        tmp.write_bytes(content)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erreur ecriture : {e}")

    ok, msg = _validate_rclone_conf(tmp)
    if not ok:
        tmp.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=msg)

    shutil.move(str(tmp), str(RCLONE_CONF))
    logger.info("[SETUP] rclone.conf uploade et valide")
    return {"status": "ok", "message": "rclone.conf valide et copie"}


@router.post("/rclone-path")
def set_rclone_path(req: RclonePathRequest):
    """Copie rclone.conf depuis un chemin accessible dans le conteneur."""
    src = Path(req.path)

    if not src.exists():
        raise HTTPException(
            status_code=400,
            detail=f"Fichier introuvable : {req.path}"
        )

    ok, msg = _validate_rclone_conf(src)
    if not ok:
        raise HTTPException(status_code=400, detail=msg)

    APP_RCLONE_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(src), str(RCLONE_CONF))
    logger.info(f"[SETUP] rclone.conf copie depuis {src}")
    return {"status": "ok", "message": "rclone.conf valide et copie"}


@router.post("/finalize")
def finalize_setup():
    """
    Finalisation AIO :
    1. Verifie que rclone.conf est present
    2. Attend que rclone (supervise par S6) monte pCloud - poll de l'API rc
    3. Marque le setup comme termine

    Plus de mount --bind, plus de restart Docker : S6 monte rclone tout seul
    des que rclone.conf apparait.
    """
    if not RCLONE_CONF.exists():
        raise HTTPException(
            status_code=400,
            detail="rclone.conf manquant - effectuer l'etape 1 d'abord"
        )

    ok, msg = _wait_pcloud_mounted(timeout=90)
    if not ok:
        raise HTTPException(status_code=500, detail=f"pCloud non monte : {msg}")

    SETUP_DONE_FILE.write_text("done")
    logger.info("[SETUP] Setup termine avec succes")
    return {"status": "ok", "message": "Installation terminee - pCloud monte"}


@router.get("/finalize/stream")
def finalize_setup_stream():
    """
    Version SSE de finalize - stream les etapes en temps reel.
    Etapes v18 : rclone_conf (check) -> pcloud_wait -> done.
    (Plus d'etapes mountpoint / rclone_restart : supprimees en AIO.)
    """
    def generate():
        def send(step: str, status: str, message: str = ""):
            import json
            data = json.dumps({"step": step, "status": status, "message": message})
            yield f"data: {data}\n\n"

        if not RCLONE_CONF.exists():
            yield from send("rclone_conf", "error", "rclone.conf manquant")
            return
        yield from send("rclone_conf", "done", "rclone.conf present")

        yield from send("pcloud_wait", "running",
                        "Montage de pCloud en cours (90s max)...")
        ok, msg = _wait_pcloud_mounted(timeout=90)
        if not ok:
            yield from send("pcloud_wait", "error", msg)
            return
        yield from send("pcloud_wait", "done", "pCloud monte")

        SETUP_DONE_FILE.write_text("done")
        yield from send("done", "done", "Installation terminee")

    return StreamingResponse(generate(), media_type="text/event-stream")
