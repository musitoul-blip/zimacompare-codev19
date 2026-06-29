"""ZimaCompare v3 — rclone.py

Pilotage de `rclone sync` / `rclone copy` via l'API « remote control » (rc)
du conteneur rclone, sans jamais lancer de commande shell.

ARCHITECTURE (voir NOTE-ARCHITECTURE-rclone-sync.md)
----------------------------------------------------
Le backend ne peut pas exécuter rclone lui-même (système en lecture seule,
pas d'accès au socket Docker). Le conteneur `zimacompare-rclone` fait tourner
rclone en mode serveur d'API (drapeau `--rc`). Ce module est le client HTTP
de cette API.

POINT CLÉ — TRANSFERT DIRECT
----------------------------
Une synchro lancée via `sync/sync` ou `sync/copy` est un transfert DIRECT
local → remote `pcloud:`. Elle ne traverse PAS le montage FUSE /network/pCloud
et n'utilise pas le cache VFS. Le disque système de 45 Go de la ZimaBoard
n'est donc jamais sollicité (cf. leçon §5.1 du contexte de reprise).

ÉTAT SÉPARÉ
-----------
Ce module gère son propre état (RcloneState), DISTINCT du ProgressState
global de config.py. La synchro rclone ne passe donc pas par les états
SCANNING/SYNCING de l'application. Un garde-fou croisé (voir main.py) interdit
de lancer une synchro rclone pendant une opération ZimaCompare et inversement.

SÉCURITÉ
--------
L'API rc est jointe sur le réseau Docker interne uniquement (port non publié).
L'authentification rc (user/pass) est lue depuis l'environnement.
"""
import os
import threading
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime
from typing import Optional

import requests

from config import setup_logging

logger = setup_logging()

# ── Configuration (lue depuis l'environnement, injectée par le compose) ──
RC_URL  = os.environ.get("RCLONE_RC_URL",  "http://127.0.0.1:5572").rstrip("/")
RC_USER = os.environ.get("RCLONE_RC_USER", "zima")
RC_PASS = os.environ.get("RCLONE_RC_PASS", "")

# Remote rclone cible — compte pCloud européen défini dans rclone.conf.
RC_REMOTE = "pcloud:"

# Le dossier source côté conteneur rclone (volume :ro du compose) correspond
# au même chemin que côté backend : /disks/... — voir §4.1 de la note.

# Timeouts des appels HTTP à l'API rc (secondes).
_HTTP_TIMEOUT_SHORT = 10     # core/version, job/status, core/stats…
_HTTP_TIMEOUT_LIST  = 60     # operations/list peut être lent sur un gros remote

# Période de rafraîchissement du suivi de progression.
_POLL_INTERVAL_S = 1.5


class RcloneError(RuntimeError):
    """Erreur de communication ou d'exécution côté API rc."""
    pass


# ─────────────────────────────────────────────────────────────────────────
#  État de la synchro rclone — séparé du ProgressState global
# ─────────────────────────────────────────────────────────────────────────
@dataclass
class RcloneState:
    rclone_state:   str   = "IDLE"      # IDLE | RUNNING | DONE | ERROR | ABORTED
    phase:          str   = ""          # "checking" | "transferring" | "" (déduit des stats)
    job_id:         int   = 0           # jobid rc de la synchro en cours
    operation:      str   = ""          # "sync" (miroir) | "copy" (sans suppr.)
    source:        str    = ""          # chemin local source
    dest:          str    = ""          # destination rclone (ex: pcloud:00_PcloudMusic)
    dry_run:        bool  = False
    progress:       int   = 0           # 0..100 (estimé, basé sur les octets)
    bytes_done:     int   = 0
    bytes_total:    int   = 0
    speed_bps:      float = 0.0         # octets/seconde instantané
    eta_seconds:    int   = 0
    transfers:      int   = 0           # fichiers transférés
    errors:         int   = 0
    checks:         int   = 0           # fichiers comparés (phase de comparaison)
    current_file:   str   = ""
    error:          str   = ""
    started_at:     str   = ""
    finished_at:    str   = ""


_state      = RcloneState()
_state_lock = threading.Lock()

# Fichier de persistance du job en cours : permet de retrouver une synchro
# toujours active côté conteneur rclone après un redémarrage du backend.
from config import APP_DATA_ROOT
_JOB_FILE = APP_DATA_ROOT / "rclone_job.json"


def get_rclone_state() -> dict:
    with _state_lock:
        return asdict(_state)


def _update_rclone_state(**kwargs):
    with _state_lock:
        for k, v in kwargs.items():
            if hasattr(_state, k):
                setattr(_state, k, v)


def is_rclone_busy() -> bool:
    """True si une synchro rclone est en cours (pour le garde-fou croisé)."""
    with _state_lock:
        return _state.rclone_state == "RUNNING"


# ─────────────────────────────────────────────────────────────────────────
#  Client HTTP de l'API rc
# ─────────────────────────────────────────────────────────────────────────
def _rc_call(command: str, payload: Optional[dict] = None,
             timeout: int = _HTTP_TIMEOUT_SHORT) -> dict:
    """Appelle une commande de l'API rc. Lève RcloneError en cas de problème.

    `command` ex: "core/version", "sync/copy", "job/status".
    """
    url = f"{RC_URL}/{command}"
    try:
        resp = requests.post(
            url,
            json=payload or {},
            auth=(RC_USER, RC_PASS),
            timeout=timeout,
        )
    except requests.exceptions.ConnectionError as e:
        raise RcloneError(
            f"Conteneur rclone injoignable ({RC_URL}). "
            f"Vérifiez qu'il est démarré. Détail : {e}"
        )
    except requests.exceptions.Timeout:
        raise RcloneError(f"Délai dépassé en appelant {command}")
    except Exception as e:
        raise RcloneError(f"Erreur d'appel rc {command} : {e}")

    if resp.status_code == 401:
        raise RcloneError(
            "Authentification rc refusée (401). Le mot de passe RCLONE_RC_PASS "
            "doit être identique côté backend et côté conteneur rclone."
        )
    if not resp.ok:
        # L'API rc renvoie un JSON {error: "..."} même sur erreur.
        detail = ""
        try:
            detail = resp.json().get("error", "")
        except Exception:
            detail = resp.text[:200]
        raise RcloneError(f"rc {command} a échoué (HTTP {resp.status_code}) : {detail}")

    try:
        return resp.json()
    except Exception as e:
        raise RcloneError(f"Réponse rc non-JSON pour {command} : {e}")


def rc_ping() -> dict:
    """Teste la disponibilité du démon rclone. Retourne sa version."""
    return _rc_call("core/version")


def rc_about(remote: str = RC_REMOTE) -> dict:
    """Quota / espace du remote (operations/about)."""
    return _rc_call("operations/about", {"fs": remote})


def rc_list_dirs(remote_path: str = RC_REMOTE) -> list:
    """Liste les SOUS-DOSSIERS d'un chemin du remote (operations/list,
    dirsOnly). `remote_path` ex: 'pcloud:' (racine) ou 'pcloud:Music'.

    Retourne une liste de noms de dossiers triée. Sert au menu déroulant
    de destination de l'UI (étape 3).
    """
    # operations/list attend fs + remote séparés.
    if ":" in remote_path:
        fs, _, sub = remote_path.partition(":")
        fs = fs + ":"
    else:
        fs, sub = RC_REMOTE, remote_path
    out = _rc_call(
        "operations/list",
        {"fs": fs, "remote": sub, "opt": {"dirsOnly": True}},
        timeout=_HTTP_TIMEOUT_LIST,
    )
    items = out.get("list", []) or []
    dirs = [it.get("Name", "") for it in items if it.get("IsDir")]
    return sorted(d for d in dirs if d)


# Chemin du montage FUSE pCloud, tel que vu par le conteneur backend.
PCLOUD_MOUNT = "/network/pCloud"


def rclone_health() -> dict:
    """Bilan de santé rclone, pour le panneau de l'onglet Cloud.

    Trois contrôles indépendants :
      1. demon  — le conteneur rclone répond-il sur son API rc ?
      2. mount  — le montage /network/pCloud est-il sain (pas un dossier
                  local vide du conteneur) ? S'appuie sur mountcheck.py.
      3. remote — le remote pcloud: est-il accessible ? Renvoie aussi le
                  quota / l'espace utilisé.

    Ne lève jamais : chaque section porte son propre statut ok/ko.
    """
    import os

    health = {
        "checked_at": datetime.now().isoformat(timespec="seconds"),
        "demon":  {"ok": False, "detail": ""},
        "mount":  {"ok": False, "detail": ""},
        "remote": {"ok": False, "detail": ""},
    }

    # ── 1. Démon rclone ─────────────────────────────────────────────────
    try:
        info = rc_ping()
        health["demon"] = {
            "ok": True,
            "version": info.get("version", "?"),
            "detail": f"rclone {info.get('version', '?')} joignable",
        }
    except RcloneError as e:
        health["demon"] = {"ok": False, "detail": str(e)}

    # ── 2. Montage /network/pCloud ──────────────────────────────────────
    # mountcheck.precheck_target() détecte si le chemin est en réalité un
    # dossier local du conteneur (montage tombé) plutôt que le FUSE pCloud.
    try:
        from mountcheck import precheck_target
        if not os.path.isdir(PCLOUD_MOUNT):
            health["mount"] = {
                "ok": False,
                "detail": f"{PCLOUD_MOUNT} introuvable",
            }
        else:
            problem = precheck_target(PCLOUD_MOUNT, expect_network=True)
            if problem:
                health["mount"] = {"ok": False, "warn": False, "detail": problem}
            else:
                # Le filesystem est bon (ce n'est pas un dossier local du
                # conteneur). On compte les entrées visibles.
                try:
                    n = len(os.listdir(PCLOUD_MOUNT))
                except OSError as e:
                    n = None
                if n == 0:
                    # Monté, mais vide : très suspect si le remote, lui, a du
                    # contenu. Cas typique : rclone a redémarré APRÈS le backend
                    # et la propagation du montage ne s'est pas refaite (§5.5).
                    # On signale un AVERTISSEMENT — pas un OK franc, pas une
                    # erreur — pour ne pas laisser croire que tout va bien.
                    health["mount"] = {
                        "ok": False, "warn": True, "entries": 0,
                        "detail": ("montage actif mais VIDE — le backend ne voit "
                                   "aucun fichier. Redémarrez le conteneur backend "
                                   "(le montage rclone a pu se refaire après lui)."),
                    }
                else:
                    detail = "montage actif"
                    if n is not None:
                        detail += f" — {n} entrée(s) à la racine"
                    health["mount"] = {"ok": True, "warn": False,
                                       "detail": detail, "entries": n}
    except Exception as e:
        health["mount"] = {"ok": False, "warn": False,
                           "detail": f"contrôle impossible : {e}"}

    # ── 3. Remote pCloud + quota ────────────────────────────────────────
    try:
        about = rc_about()
        total = about.get("total")
        used  = about.get("used")
        free  = about.get("free")
        health["remote"] = {
            "ok": True,
            "total_bytes": total,
            "used_bytes":  used,
            "free_bytes":  free,
            "detail": "remote pcloud: accessible",
        }
    except RcloneError as e:
        health["remote"] = {"ok": False, "detail": str(e)}

    health["all_ok"] = (health["demon"]["ok"]
                        and health["mount"]["ok"]
                        and health["remote"]["ok"])
    return health


# ─────────────────────────────────────────────────────────────────────────
#  Persistance légère du job en cours
# ─────────────────────────────────────────────────────────────────────────
def _save_job_file():
    """Persiste l'identité du job en cours (pour reprise après reboot backend)."""
    import json
    try:
        APP_DATA_ROOT.mkdir(parents=True, exist_ok=True)
        with _state_lock:
            data = {
                "job_id":     _state.job_id,
                "operation":  _state.operation,
                "source":     _state.source,
                "dest":       _state.dest,
                "dry_run":    _state.dry_run,
                "started_at": _state.started_at,
            }
        tmp = _JOB_FILE.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        tmp.replace(_JOB_FILE)
    except Exception:
        pass


def _clear_job_file():
    try:
        if _JOB_FILE.exists():
            _JOB_FILE.unlink()
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────
#  Lancement et suivi d'une synchro
# ─────────────────────────────────────────────────────────────────────────
_stop_event = threading.Event()


def _stats_to_state(stats: dict):
    """Traduit la réponse core/stats de rclone dans le RcloneState."""
    bytes_done  = int(stats.get("bytes", 0) or 0)
    bytes_total = int(stats.get("totalBytes", 0) or 0)
    speed       = float(stats.get("speed", 0) or 0)
    eta         = stats.get("eta")
    eta_i       = int(eta) if isinstance(eta, (int, float)) else 0
    transfers   = int(stats.get("transfers", 0) or 0)
    errors      = int(stats.get("errors", 0) or 0)
    checks      = int(stats.get("checks", 0) or 0)

    # Progression : rclone ne connaît totalBytes qu'après la phase de scan
    # initiale. Tant qu'il vaut 0, on reste à une progression indéterminée (0).
    if bytes_total > 0:
        progress = min(100, int(bytes_done / bytes_total * 100))
    else:
        progress = 0

    # Fichier en cours : premier élément de la liste "transferring".
    current = ""
    transferring = stats.get("transferring") or []
    if transferring:
        current = transferring[0].get("name", "") or ""

    # Phase déduite : rclone « marche » d'abord dans l'arborescence en comparant
    # les fichiers (checks qui grimpent) AVANT de transférer. En dry-run, aucun
    # octet n'est réellement copié : la barre saute à 100 % tout de suite alors
    # que la comparaison continue. On expose donc une phase explicite pour que
    # l'UI ne paraisse pas figée.
    #   - "transferring" : un fichier est en cours de transfert réel.
    #   - "checking"     : pas de transfert en cours mais des comparaisons
    #                       progressent (ou rien n'a encore été transféré).
    if transferring:
        phase = "transferring"
    elif checks > 0 and transfers == 0:
        phase = "checking"
    elif speed > 0:
        phase = "transferring"
    else:
        # Cas ambigu (peu d'activité) : on garde "checking" tant qu'il reste
        # potentiellement des comparaisons, sinon vide.
        phase = "checking" if checks > 0 else ""

    _update_rclone_state(
        phase=phase,
        progress=progress, bytes_done=bytes_done, bytes_total=bytes_total,
        speed_bps=speed, eta_seconds=eta_i, transfers=transfers,
        errors=errors, checks=checks, current_file=current,
    )


def _monitor_job(job_id: int):
    """Thread de suivi : interroge l'API rc jusqu'à la fin du job."""
    logger.info(f"[RCLONE] Suivi du job {job_id} démarré.")
    try:
        while not _stop_event.is_set():
            time.sleep(_POLL_INTERVAL_S)

            # 1. Statistiques globales (filtrées sur ce job via le group).
            try:
                stats = _rc_call("core/stats", {"group": f"job/{job_id}"})
                _stats_to_state(stats)
            except RcloneError as e:
                # Une erreur de stats n'est pas fatale : on continue à suivre.
                logger.warning(f"[RCLONE] core/stats : {e}")

            # 2. Statut du job.
            try:
                status = _rc_call("job/status", {"jobid": job_id})
            except RcloneError as e:
                logger.error(f"[RCLONE] job/status injoignable : {e}")
                _update_rclone_state(rclone_state="ERROR",
                                     error=f"Suivi interrompu : {e}",
                                     finished_at=datetime.now().isoformat(timespec="seconds"))
                _clear_job_file()
                return

            if status.get("finished"):
                success = status.get("success", False)
                duration = status.get("duration", 0)
                err_txt  = status.get("error", "") or ""
                # Stats finales.
                try:
                    final = _rc_call("core/stats", {"group": f"job/{job_id}"})
                    _stats_to_state(final)
                except RcloneError:
                    pass
                finished = datetime.now().isoformat(timespec="seconds")
                if success:
                    logger.info(f"[RCLONE] Job {job_id} terminé avec succès "
                                f"en {duration:.0f}s.")
                    _update_rclone_state(rclone_state="DONE", progress=100,
                                         phase="", current_file="", error="",
                                         finished_at=finished)
                elif _stop_event.is_set():
                    # L'utilisateur a demandé l'arrêt : job/stop a fait échouer
                    # le job avec « context canceled ». Ce n'est PAS une erreur
                    # — on le classe proprement comme interruption volontaire.
                    logger.warning(f"[RCLONE] Job {job_id} interrompu à la "
                                   f"demande de l'utilisateur.")
                    _update_rclone_state(rclone_state="ABORTED", phase="", current_file="",
                                         error="", finished_at=finished)
                else:
                    logger.error(f"[RCLONE] Job {job_id} en échec : {err_txt}")
                    _update_rclone_state(rclone_state="ERROR", phase="",
                                         error=err_txt or "Échec de la synchro rclone",
                                         current_file="", finished_at=finished)
                _clear_job_file()
                return

        # Sortie de boucle par _stop_event → abandon demandé.
        logger.warning(f"[RCLONE] Suivi du job {job_id} arrêté (abandon).")
        _update_rclone_state(rclone_state="ABORTED", phase="", current_file="",
                             finished_at=datetime.now().isoformat(timespec="seconds"))
        _clear_job_file()

    except Exception as e:
        logger.error(f"[RCLONE] Erreur fatale du suivi : {e}", exc_info=True)
        _update_rclone_state(rclone_state="ERROR", phase="", error=str(e),
                             finished_at=datetime.now().isoformat(timespec="seconds"))
        _clear_job_file()


def start_rclone_sync(source: str, dest: str, *, dry_run: bool = True,
                      mirror: bool = False) -> dict:
    """Démarre une synchro rclone (asynchrone côté rclone).

    - `source` : chemin local (doit être visible par le conteneur rclone,
      typiquement sous /disks/...).
    - `dest`   : destination rclone, ex: 'pcloud:00_PcloudMusic'. Si la chaîne
      ne contient pas de ':', on la considère comme un sous-dossier de pcloud:.
    - `dry_run`: simulation, aucune écriture.
    - `mirror` : True → sync/sync (efface côté destination ce qui n'est plus
      dans la source). False → sync/copy (n'efface rien — mode sûr par défaut,
      cf. §5.6 du contexte).

    Retourne {ok, job_id, operation} ou lève RcloneError.
    """
    with _state_lock:
        if _state.rclone_state == "RUNNING":
            raise RcloneError("Une synchro rclone est déjà en cours.")

    # Normalisation de la destination.
    dest = dest.strip()
    if not dest:
        raise RcloneError("Destination vide.")
    if ":" not in dest:
        # Pas de remote explicite → sous-dossier de pcloud:
        dest = RC_REMOTE + dest.lstrip("/")

    if not source or not source.startswith("/"):
        raise RcloneError(f"Source invalide : {source!r}")

    # Le démon doit répondre avant qu'on lance quoi que ce soit.
    rc_ping()

    operation = "sync" if mirror else "copy"
    command   = "sync/sync" if mirror else "sync/copy"

    # SizeOnly : rclone compare les fichiers sur la SEULE taille, en ignorant
    # la date de modification. Indispensable avec pCloud, qui ne conserve pas
    # les dates à l'identique : sans ça, rclone signalerait des centaines de
    # fichiers « différents » dont seule la date varie (contenu identique).
    # Cela aligne le mode complet sur la logique de ZimaCompare (le contenu
    # prime, pas la date).
    payload = {
        "srcFs":  source,
        "dstFs":  dest,
        "_async": True,                    # rclone renvoie un jobid immédiatement
        "_config": {"DryRun": bool(dry_run), "SizeOnly": True},
    }

    logger.info(f"[RCLONE] Démarrage {operation.upper()} "
                f"{'(DRY-RUN) ' if dry_run else ''}"
                f"source={source} dest={dest} — comparaison sur la taille")

    result = _rc_call(command, payload)
    job_id = result.get("jobid")
    if not job_id:
        raise RcloneError(f"L'API rc n'a pas renvoyé de jobid : {result}")

    _stop_event.clear()
    now = datetime.now().isoformat(timespec="seconds")
    with _state_lock:
        _state.rclone_state = "RUNNING"
        _state.phase        = "checking"
        _state.job_id       = int(job_id)
        _state.operation    = operation
        _state.source       = source
        _state.dest         = dest
        _state.dry_run      = bool(dry_run)
        _state.progress     = 0
        _state.bytes_done   = 0
        _state.bytes_total  = 0
        _state.speed_bps    = 0.0
        _state.eta_seconds  = 0
        _state.transfers    = 0
        _state.errors       = 0
        _state.checks       = 0
        _state.current_file = ""
        _state.error        = ""
        _state.started_at   = now
        _state.finished_at  = ""

    _save_job_file()

    t = threading.Thread(target=_monitor_job, args=(int(job_id),), daemon=True)
    t.start()

    return {"ok": True, "job_id": int(job_id), "operation": operation,
            "dry_run": bool(dry_run), "source": source, "dest": dest}


def abort_rclone_sync() -> dict:
    """Arrête la synchro rclone en cours (job/stop + arrêt du suivi)."""
    with _state_lock:
        if _state.rclone_state != "RUNNING":
            raise RcloneError("Aucune synchro rclone en cours.")
        job_id = _state.job_id

    logger.warning(f"[RCLONE] Arrêt demandé du job {job_id}.")
    # On stoppe d'abord le job côté rclone…
    try:
        _rc_call("job/stop", {"jobid": job_id})
    except RcloneError as e:
        logger.warning(f"[RCLONE] job/stop : {e}")
    # …puis on lève le drapeau qui arrête le thread de suivi.
    _stop_event.set()
    return {"ok": True, "job_id": job_id, "status": "aborting"}


# ─────────────────────────────────────────────────────────────────────────
#  Mode rapide — synchro à partir d'un scan ZimaCompare (--files-from)
# ─────────────────────────────────────────────────────────────────────────
import csv as _csv

# Âge maximal d'un scan pour le mode rapide avant avertissement (heures).
SCAN_FRESHNESS_HOURS = 24


def scan_summary_for_fast_sync() -> dict:
    """Inspecte le dernier scan (scan_results.csv + état) pour préparer un
    éventuel mode rapide. Ne lance rien — sert à informer l'UI.

    Retourne :
      - available  : un scan exploitable existe-t-il ?
      - source/target/method : couple et méthode du dernier scan
      - to_sync_count : nombre de fichiers new+different (= à transférer)
      - scan_age_hours : âge du scan
      - stale : True si le scan dépasse le seuil de fraîcheur
      - reason : message si non disponible
    """
    from config import SCAN_CSV, get_state

    out = {
        "available": False, "source": "", "target": "", "method": "",
        "to_sync_count": 0, "new_count": 0, "different_count": 0, "deleted_count": 0,
        "scan_age_hours": None, "stale": False, "scanned_at": "", "reason": "",
    }

    if not SCAN_CSV.exists():
        out["reason"] = "Aucun scan disponible — lancez d'abord un scan."
        return out

    # Âge du scan, d'après la date du fichier CSV.
    try:
        mtime = SCAN_CSV.stat().st_mtime
        age_h = (time.time() - mtime) / 3600.0
        out["scan_age_hours"] = round(age_h, 1)
        out["scanned_at"] = datetime.fromtimestamp(mtime).isoformat(timespec="seconds")
        out["stale"] = age_h > SCAN_FRESHNESS_HOURS
    except OSError:
        pass

    # Couple source/cible/méthode : repris de l'état applicatif courant.
    st = get_state()
    out["source"] = st.get("source", "")
    out["target"] = st.get("target", "")
    out["method"] = st.get("method", "")

    # Comptage des fichiers new + different (hors dossiers).
    new = diff = deleted = 0
    try:
        with open(SCAN_CSV, newline="", encoding="utf-8") as f:
            reader = _csv.DictReader(f, delimiter=";")
            for row in reader:
                if str(row.get("is_dir", "")).strip().lower() in ("true", "1"):
                    continue
                status = (row.get("status") or "").strip()
                if status == "new":
                    new += 1
                elif status == "different":
                    diff += 1
                elif status == "deleted":
                    deleted += 1
    except Exception as e:
        out["reason"] = f"Lecture du scan impossible : {e}"
        return out

    out["new_count"] = new
    out["different_count"] = diff
    out["deleted_count"] = deleted
    out["to_sync_count"] = new + diff
    out["available"] = True
    return out


# Dossier de configuration rclone, PARTAGÉ entre le conteneur backend et le
# conteneur rclone. Le fichier --files-from doit y être déposé : il est écrit
# par le backend mais LU par le conteneur rclone.
#   - côté backend  : RCLONE_SHARED_DIR  (volume ajouté au compose)
#   - côté rclone   : /config/rclone     (déjà monté)
# Le backend écrit dans RCLONE_SHARED_DIR ; il transmet à rclone le chemin
# vu par le conteneur rclone (RCLONE_REMOTE_CONFIG_DIR).
RCLONE_SHARED_DIR        = os.environ.get(
    "RCLONE_SHARED_DIR", "/config/rclone")
RCLONE_REMOTE_CONFIG_DIR = "/config/rclone"
_FILES_FROM_NAME         = "rclone_files_from.txt"


def _build_files_from_list(expect_source: str) -> tuple:
    """Construit la liste des fichiers new+different à partir de scan_results.csv.

    `expect_source` : la source attendue (celle demandée pour la synchro) —
    sert de garde-fou : on refuse si le scan ne porte pas sur cette source.

    Le fichier liste est écrit dans le dossier rclone PARTAGÉ, pour que le
    conteneur rclone puisse le lire via --files-from.

    Retourne (chemin_vu_par_rclone, nombre_de_fichiers).
    Lève RcloneError si le scan est absent, incohérent, ou vide.
    """
    from config import SCAN_CSV, get_state

    if not SCAN_CSV.exists():
        raise RcloneError("Aucun scan disponible — lancez d'abord un scan.")

    # Garde-fou de concordance : le scan doit porter sur la même source.
    st = get_state()
    scan_source = st.get("source", "")
    if scan_source and expect_source and scan_source != expect_source:
        raise RcloneError(
            f"Le dernier scan porte sur une autre source ({scan_source}) "
            f"que celle demandée ({expect_source}). Relancez un scan sur le "
            f"bon couple avant d'utiliser le mode rapide."
        )

    # Extraction des chemins new + different (hors dossiers).
    rels = []
    try:
        with open(SCAN_CSV, newline="", encoding="utf-8") as f:
            reader = _csv.DictReader(f, delimiter=";")
            for row in reader:
                if str(row.get("is_dir", "")).strip().lower() in ("true", "1"):
                    continue
                status = (row.get("status") or "").strip()
                if status in ("new", "different"):
                    rel = (row.get("relative_path") or "").strip()
                    if rel:
                        rels.append(rel)
    except Exception as e:
        raise RcloneError(f"Lecture du scan impossible : {e}")

    if not rels:
        raise RcloneError(
            "Le dernier scan ne signale aucun fichier à transférer "
            "(0 nouveau, 0 différent) — rien à synchroniser."
        )

    # Écriture du fichier liste dans le dossier PARTAGÉ avec le conteneur rclone.
    shared = os.path.abspath(RCLONE_SHARED_DIR)
    if not os.path.isdir(shared):
        raise RcloneError(
            f"Dossier d'échange rclone introuvable côté backend ({shared}). "
            f"Le volume partagé n'est pas monté — vérifiez le docker-compose."
        )
    list_path_backend = os.path.join(shared, _FILES_FROM_NAME)
    try:
        with open(list_path_backend, "w", encoding="utf-8") as f:
            for rel in rels:
                # rclone --files-from attend des chemins relatifs séparés par
                # des sauts de ligne ; les chemins du CSV utilisent déjà '/'.
                f.write(rel.replace("\\", "/") + "\n")
    except OSError as e:
        raise RcloneError(f"Écriture de la liste impossible : {e}")

    # Chemin tel que le conteneur RCLONE le voit (c'est lui qui lit le fichier).
    list_path_rclone = f"{RCLONE_REMOTE_CONFIG_DIR}/{_FILES_FROM_NAME}"
    return list_path_rclone, len(rels)


def _build_deletes_from_list(expect_source: str) -> list:
    """Liste des chemins relatifs en statut 'deleted' du dernier scan.
    Meme garde-fou de concordance source que _build_files_from_list.
    Retourne [] si aucun (la passe de suppression ne fera alors rien)."""
    from config import SCAN_CSV, get_state
    if not SCAN_CSV.exists():
        return []
    st = get_state()
    scan_source = st.get("source", "")
    if scan_source and expect_source and scan_source != expect_source:
        raise RcloneError(
            f"Le dernier scan porte sur une autre source ({scan_source}) "
            f"que celle demandee ({expect_source})."
        )
    rels = []
    try:
        with open(SCAN_CSV, newline="", encoding="utf-8") as f:
            reader = _csv.DictReader(f, delimiter=";")
            for row in reader:
                if str(row.get("is_dir", "")).strip().lower() in ("true", "1"):
                    continue
                if (row.get("status") or "").strip() == "deleted":
                    rel = (row.get("relative_path") or "").strip()
                    if rel:
                        rels.append(rel.replace("\\", "/"))
    except Exception as e:
        raise RcloneError(f"Lecture du scan impossible : {e}")
    return rels


def _split_dest_fs(dest: str) -> tuple:
    """'pcloud:00_PcloudMusic' -> ('pcloud:', '00_PcloudMusic')."""
    d = (dest or "").strip()
    if ":" not in d:
        return RC_REMOTE, d.lstrip("/")
    i = d.index(":")
    return d[:i + 1], d[i + 1:].lstrip("/")


def _rc_deletefile(fs: str, remote: str, dry_run: bool) -> bool:
    """Supprime UN fichier du remote via operations/deletefile.
    dry_run -> log seulement (pas d'appel). Retourne True si supprime (ou simule)."""
    if dry_run:
        logger.info(f"[RCLONE][DELETE] (DRY-RUN) {fs}{remote}")
        return True
    try:
        _rc_call("operations/deletefile", {"fs": fs, "remote": remote})
        logger.info(f"[RCLONE][DELETE] supprime {fs}{remote}")
        return True
    except RcloneError as e:
        logger.error(f"[RCLONE][DELETE] echec {fs}{remote} : {e}")
        return False


def _orchestrate_deletes_after_copy(dest: str, rels: list, dry_run: bool):
    """Thread : attend la fin de la copie ; si DONE, supprime cote pCloud les
    fichiers 'deleted' du scan (liste bornee). Si ERROR/ABORTED -> ne supprime
    rien (securite : pas de suppression si la copie n'a pas abouti)."""
    # Attente de fin de copie (rclone_state quitte RUNNING).
    while True:
        time.sleep(_POLL_INTERVAL_S)
        with _state_lock:
            stt = _state.rclone_state
        if stt != "RUNNING":
            break
        if _stop_event.is_set():
            logger.warning("[RCLONE][DELETE] arret demande avant la passe de suppression.")
            return
    if stt != "DONE":
        logger.warning(f"[RCLONE][DELETE] copie terminee en '{stt}' (non DONE) "
                       f"-> suppressions ignorees ({len(rels)} fichier(s) non traite(s)).")
        return
    fs, base = _split_dest_fs(dest)
    prefix = (base + "/") if base else ""
    done = 0
    errors = 0
    logger.info(f"[RCLONE][DELETE] Debut passe de suppression "
                f"{'(DRY-RUN) ' if dry_run else ''}: {len(rels)} fichier(s) cible(s).")
    for rel in rels:
        if _stop_event.is_set():
            logger.warning(f"[RCLONE][DELETE] interrompu : {done}/{len(rels)} traite(s).")
            break
        ok = _rc_deletefile(fs, prefix + rel, dry_run)
        if ok:
            done += 1
        else:
            errors += 1
    logger.info(f"[RCLONE][DELETE] Passe terminee : {done} supprime(s), "
                f"{errors} echec(s){' (DRY-RUN)' if dry_run else ''}.")


def start_rclone_fast_sync(source: str, dest: str, *,
                           dry_run: bool = True,
                           mirror_deletes: bool = False) -> dict:
    """Mode rapide : transfère UNIQUEMENT les fichiers new+different du dernier
    scan ZimaCompare, via --files-from. rclone ne re-compare rien.

    Toujours en mode `copy` (jamais miroir) : une liste de fichiers à copier
    ne dit rien des fichiers à supprimer. Pas de contrôle post-copie.
    """
    with _state_lock:
        if _state.rclone_state == "RUNNING":
            raise RcloneError("Une synchro rclone est déjà en cours.")

    # Normalisation de la destination (idem start_rclone_sync).
    dest = dest.strip()
    if not dest:
        raise RcloneError("Destination vide.")
    if ":" not in dest:
        dest = RC_REMOTE + dest.lstrip("/")
    if not source or not source.startswith("/"):
        raise RcloneError(f"Source invalide : {source!r}")

    # Construction de la liste (+ garde-fou de concordance de source).
    list_path, n_files = _build_files_from_list(source)

    # Le démon doit répondre.
    rc_ping()

    # sync/copy avec le filtre FilesFrom. --no-traverse (NoTraverse) accélère
    # nettement quand on copie peu de fichiers vers une grosse destination.
    payload = {
        "srcFs":  source,
        "dstFs":  dest,
        "_async": True,
        "_config": {"DryRun": bool(dry_run), "NoTraverse": True},
        "_filter": {"FilesFrom": [list_path]},
    }

    logger.info(f"[RCLONE] Démarrage COPY RAPIDE "
                f"{'(DRY-RUN) ' if dry_run else ''}"
                f"source={source} dest={dest} — {n_files} fichier(s) "
                f"d'après le dernier scan")

    result = _rc_call("sync/copy", payload)
    job_id = result.get("jobid")
    if not job_id:
        raise RcloneError(f"L'API rc n'a pas renvoyé de jobid : {result}")

    _stop_event.clear()
    now = datetime.now().isoformat(timespec="seconds")
    with _state_lock:
        _state.rclone_state = "RUNNING"
        _state.phase        = "transferring"   # mode rapide : pas de phase checking
        _state.job_id       = int(job_id)
        _state.operation    = "copy"
        _state.source       = source
        _state.dest         = dest
        _state.dry_run      = bool(dry_run)
        _state.progress     = 0
        _state.bytes_done   = 0
        _state.bytes_total  = 0
        _state.speed_bps    = 0.0
        _state.eta_seconds  = 0
        _state.transfers    = 0
        _state.errors       = 0
        _state.checks       = 0
        _state.current_file = ""
        _state.error        = ""
        _state.started_at   = now
        _state.finished_at  = ""

    _save_job_file()

    t = threading.Thread(target=_monitor_job, args=(int(job_id),), daemon=True)
    t.start()

    # Passe de suppression (option B) : uniquement si demande ET liste non vide.
    deletes_count = 0
    if mirror_deletes:
        try:
            _dels = _build_deletes_from_list(source)
        except RcloneError as e:
            logger.warning(f"[RCLONE][DELETE] liste suppressions indisponible : {e}")
            _dels = []
        deletes_count = len(_dels)
        if _dels:
            logger.info(f"[RCLONE][DELETE] {deletes_count} suppression(s) "
                        f"programmee(s) apres la copie.")
            dt = threading.Thread(target=_orchestrate_deletes_after_copy,
                                  args=(dest, _dels, bool(dry_run)), daemon=True)
            dt.start()

    return {"ok": True, "job_id": int(job_id), "operation": "copy",
            "mode": "fast", "dry_run": bool(dry_run),
            "files_count": n_files, "deletes_count": deletes_count,
            "source": source, "dest": dest}


# ─────────────────────────────────────────────────────────────────────────
#  T5 — Empreintes serveur pCloud (SHA1) sans téléchargement.
#  operations/list (showHash) via l'API rc. Découpage par dossier de premier
#  niveau → progression, interruptible, réponses bornées, pas de timeout.
# ─────────────────────────────────────────────────────────────────────────
_HTTP_TIMEOUT_HASH = 600   # un dossier d'album (empreintes serveur) — généreux


def map_mount_to_remote(target_path: str):
    """'/network/pCloud/00_PcloudMusic' -> ('pcloud:', '00_PcloudMusic').
    Retourne None si le chemin n'est pas sous le montage pCloud."""
    t = (target_path or "").rstrip("/")
    base = PCLOUD_MOUNT.rstrip("/")
    if t == base:
        return RC_REMOTE, ""
    if t.startswith(base + "/"):
        return RC_REMOTE, t[len(base) + 1:]
    return None


def _list_remote(fs, remote, opt, timeout=_HTTP_TIMEOUT_HASH):
    out = _rc_call("operations/list",
                   {"fs": fs, "remote": remote, "opt": opt}, timeout=timeout)
    return out.get("list", []) or []


def fetch_remote_hashes(target_path, hash_type="sha1",
                        progress_cb=None, stop_event=None) -> dict:
    """Empreintes serveur (SHA1 par défaut) de tous les fichiers sous
    `target_path` (montage pCloud), via l'API rc, SANS télécharger.
    Retourne {rel: hash}, rel relatif à `target_path` (aligné sur le scanner)."""
    mapped = map_mount_to_remote(target_path)
    if mapped is None:
        raise RcloneError(
            f"Cible {target_path!r} hors du montage pCloud ({PCLOUD_MOUNT}) "
            f"— empreintes serveur indisponibles.")
    fs, subpath = mapped
    prefix = (subpath + "/") if subpath else ""
    rc_ping()

    files_opt = {"recurse": False, "filesOnly": True,
                 "showHash": True, "hashTypes": [hash_type]}
    recurse_opt = {"recurse": True, "filesOnly": True,
                   "showHash": True, "hashTypes": [hash_type]}
    result = {}

    def _ingest(items):
        for it in items:
            if it.get("IsDir"):
                continue
            p = it.get("Path", "")
            rel = p[len(prefix):] if prefix and p.startswith(prefix) else p
            h = (it.get("Hashes") or {}).get(hash_type, "")
            result[rel] = h or "error"

    # fichiers directement à la racine de la cible
    _ingest(_list_remote(fs, subpath, files_opt, _HTTP_TIMEOUT_LIST))

    # dossiers de premier niveau (un appel récursif chacun)
    top = _rc_call("operations/list",
                   {"fs": fs, "remote": subpath, "opt": {"dirsOnly": True}},
                   timeout=_HTTP_TIMEOUT_LIST)
    dirs = sorted(d.get("Name", "") for d in (top.get("list") or [])
                  if d.get("IsDir") and d.get("Name"))
    n = len(dirs)
    for i, d in enumerate(dirs):
        if stop_event is not None and stop_event.is_set():
            raise RcloneError("Récupération des empreintes interrompue.")
        sub = f"{subpath}/{d}" if subpath else d
        _ingest(_list_remote(fs, sub, recurse_opt))
        if progress_cb:
            try:
                progress_cb(i + 1, n, d, len(result))
            except Exception:
                pass
    return result
