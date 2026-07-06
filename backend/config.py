"""ZimaCompare v18 — Configuration, état global, logging, historique.

Note : /app_data est désormais monté depuis /DATA/AppData/zimacompare-v3/data
(au lieu de /DATA/AppData/ZimaCompare). Tous les fichiers de l'app sont donc
regroupés dans le dossier projet pour ne sauvegarder qu'une seule arbo.
"""
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import List

APP_DATA_ROOT      = Path("/app_data")
APP_VERSION         = "3.18.0"
CONFIG_FILE        = APP_DATA_ROOT / "config.json"
STATE_FILE         = APP_DATA_ROOT / "app_state.json"
SCAN_CSV           = APP_DATA_ROOT / "scan_results.csv"
IGNORED_FILES_JSON = APP_DATA_ROOT / "ignored_files.json"   # v3.13
REPORTS_DIR        = APP_DATA_ROOT / "reports"
TEMP_DIR           = APP_DATA_ROOT / "temp"
LOG_FILE           = APP_DATA_ROOT / "zimacompare.log"
ZIMAIGNORE_FILE    = APP_DATA_ROOT / ".zimaignore"
PATHS_HISTORY_FILE = APP_DATA_ROOT / "paths_history.json"
HASH_CACHE_FILE    = APP_DATA_ROOT / "hash_cache.json"

VALID_PREFIXES  = ("/disks/", "/network/")
# v3.12 — Patterns par défaut au format gitignore (pathspec / gitwildmatch).
# Note historique : avant v3.12 le matcher était un simple `p in name` buggué.
# Le pattern `.tmp` ancien matchait n'importe quel fichier contenant `.tmp`
# dans son nom (faux positifs). On passe à `*.tmp` qui est l'intention.
DEFAULT_IGNORE  = [
    ".DS_Store",       # macOS Finder
    "Thumbs.db",       # Windows Explorer
    "$RECYCLE.BIN",    # Windows Trash
    ".Trash",          # macOS Trash
    ".Trash-*",        # Linux Trash (uid-spécifique)
    "@eaDir",          # Synology metadata
    "*.tmp",           # fichiers temporaires
    "desktop.ini",     # Windows
    ".zima_write_test",
]
HISTORY_MAX = 15
ZIMAIGNORE_MAX_BYTES = 64 * 1024  # 64 KiB — garde-fou contre injection volumineuse


class AppState(str, Enum):
    IDLE      = "IDLE"
    SCANNING  = "SCANNING"
    COMPARING = "COMPARING"
    SYNCING   = "SYNCING"
    VERIFYING = "VERIFYING"  # NEW v3.4 : phase de vérification post-sync
    ERROR     = "ERROR"


@dataclass
class ProgressState:
    app_state:       str   = AppState.IDLE
    progress:        int   = 0
    total:           int   = 0
    processed:       int   = 0
    current_file:    str   = ""
    fps:             float = 0.0
    eta_seconds:     int   = 0
    source:          str   = ""
    target:          str   = ""
    method:          str   = "fast"
    error:           str   = ""
    dry_run:         bool  = True
    new_count:       int   = 0
    different_count: int   = 0
    deleted_count:   int   = 0
    identical_count: int   = 0
    sync_done:       int   = 0
    sync_errors:     int   = 0
    sync_simulated:  int   = 0
    bytes_to_copy:   int   = 0
    scan_done:       bool  = False
    target_changed: bool = False
    target_sig: str = ""
    scan_filter: str = ""
    scan_seq: int = 0
    scan_ext: str = ""
    # NEW v3.4
    source_changed:  bool  = False    # mtime du dossier source a changé pendant le scan
    source_warning:  str   = ""        # détail du warning de modification source
    sync_verified:   str   = ""        # "" | "pending" | "ok" | "failed"
    sync_verified_msg: str = ""        # détail (ex : "3 fichiers diffèrent encore")
    last_sync_at:    str   = ""        # ISO timestamp du dernier sync réel
    # NEW v3.12
    ignored_count:   int   = 0         # nombre d'entrées filtrées par .zimaignore pendant le scan


_state      = ProgressState()
_state_lock = threading.Lock()


def get_state() -> dict:
    with _state_lock:
        return asdict(_state)


def update_state(**kwargs):
    with _state_lock:
        if kwargs.get("scan_done") is True and not _state.scan_done:
            kwargs["scan_seq"] = _state.scan_seq + 1
        for k, v in kwargs.items():
            if hasattr(_state, k):
                setattr(_state, k, v)
    _persist_state()


def _persist_state():
    try:
        APP_DATA_ROOT.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(asdict(_state), f)
        tmp.replace(STATE_FILE)
    except Exception:
        pass


def load_persisted_state():
    if not STATE_FILE.exists():
        return
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
        with _state_lock:
            for k, v in data.items():
                if hasattr(_state, k):
                    setattr(_state, k, v)
            if _state.app_state in (AppState.SCANNING, AppState.SYNCING,
                                     AppState.COMPARING, AppState.VERIFYING):
                _state.app_state = AppState.ERROR
                _state.error = "Backend redémarré pendant l'opération — relancez manuellement."
                _persist_state()
    except Exception:
        pass


@dataclass
class AppConfig:
    comparison_method: str   = "fast"
    verify_after_copy: bool  = True
    dry_run:           bool  = True
    max_copy_workers:  int   = 2
    mirror_deletes:    bool  = True
    chunk_size_mb:     int   = 4
    auto_verify_sync:  bool  = True    # NEW v3.4

    def save(self):
        ensure_dirs()
        tmp = CONFIG_FILE.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(asdict(self), f, indent=2)
        tmp.replace(CONFIG_FILE)

    @classmethod
    def load(cls) -> "AppConfig":
        if CONFIG_FILE.exists():
            try:
                with open(CONFIG_FILE) as f:
                    data = json.load(f)
                return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
            except Exception:
                pass
        cfg = cls()
        cfg.save()
        return cfg


# ── v3.12 — Patterns d'ignore au format gitignore (pathspec) ─────────
try:
    import pathspec  # ajouté dans requirements.txt v3.12
    _PATHSPEC_OK = True
except Exception:
    _PATHSPEC_OK = False


def _ensure_zimaignore_file():
    """Crée le fichier .zimaignore avec les défauts si absent."""
    if not ZIMAIGNORE_FILE.exists():
        header = (
            "# ZimaCompare .zimaignore — patterns gitignore-style\n"
            "# Documentation : https://git-scm.com/docs/gitignore\n"
            "# Syntaxe :\n"
            "#   *.tmp           → tout fichier .tmp\n"
            "#   **/cache/       → dossiers cache à n'importe quelle profondeur\n"
            "#   /build/         → dossier build à la racine de la source\n"
            "#   !keep.tmp       → négation (réincluse)\n"
            "#\n"
        )
        body = "\n".join(DEFAULT_IGNORE) + "\n"
        try:
            ZIMAIGNORE_FILE.parent.mkdir(parents=True, exist_ok=True)
            ZIMAIGNORE_FILE.write_text(header + body, encoding="utf-8")
        except Exception:
            pass


def read_ignore_text() -> str:
    """Retourne le contenu brut du fichier .zimaignore (avec commentaires)."""
    _ensure_zimaignore_file()
    try:
        return ZIMAIGNORE_FILE.read_text(encoding="utf-8")
    except Exception:
        return "\n".join(DEFAULT_IGNORE) + "\n"


def parse_ignore_lines(text: str) -> List[str]:
    """Extrait les patterns actifs (sans commentaires ni lignes vides)."""
    return [l.strip() for l in text.splitlines()
            if l.strip() and not l.lstrip().startswith("#")]


def load_ignore_patterns() -> List[str]:
    """Retourne juste la liste des patterns actifs. Conservé pour compat.
    Pour le matching, préférer `compile_ignore_spec()`."""
    return parse_ignore_lines(read_ignore_text())


def compile_ignore_spec(patterns: List[str] = None):
    """Compile un PathSpec gitwildmatch à partir des patterns donnés.
    Si pathspec n'est pas installé, retourne None et le scanner retombera
    sur un matching naïf (exact-name only).
    """
    if not _PATHSPEC_OK:
        return None
    if patterns is None:
        patterns = load_ignore_patterns()
    try:
        return pathspec.PathSpec.from_lines("gitwildmatch", patterns)
    except Exception:
        return None


def save_ignore_text(text: str) -> dict:
    """Écrit le contenu fourni dans .zimaignore (avec garde-fous).
    Retourne {ok, errors, patterns_active, bytes}.
    """
    out = {"ok": False, "errors": [], "patterns_active": 0, "bytes": 0}
    if not isinstance(text, str):
        out["errors"].append("Contenu invalide (non textuel)")
        return out
    data = text.encode("utf-8")
    if len(data) > ZIMAIGNORE_MAX_BYTES:
        out["errors"].append(
            f"Fichier trop volumineux ({len(data)} octets, max {ZIMAIGNORE_MAX_BYTES})"
        )
        return out
    # Validation syntaxique — on tente de compiler.
    patterns = parse_ignore_lines(text)
    if _PATHSPEC_OK:
        try:
            pathspec.PathSpec.from_lines("gitwildmatch", patterns)
        except Exception as e:
            out["errors"].append(f"Erreur de parsing : {e}")
            return out
    try:
        APP_DATA_ROOT.mkdir(parents=True, exist_ok=True)
        tmp = ZIMAIGNORE_FILE.with_suffix(".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(ZIMAIGNORE_FILE)
    except Exception as e:
        out["errors"].append(f"Écriture impossible : {e}")
        return out
    out["ok"] = True
    out["patterns_active"] = len(patterns)
    out["bytes"] = len(data)
    return out


def ignore_match(spec, rel_path: str, is_dir: bool) -> bool:
    """Test unifié : True si le chemin doit être ignoré.

    `rel_path` est relatif à la racine scannée (sans slash initial).
    `is_dir` doit être True pour un dossier (sémantique gitignore distingue
    `cache/` de `cache`).
    """
    if spec is None:
        return False
    # Convention gitignore : un dossier matche avec un slash final.
    candidate = rel_path + "/" if is_dir else rel_path
    try:
        return spec.match_file(candidate)
    except Exception:
        return False


def compile_individual_specs(patterns: List[str] = None):
    """Compile une liste de (pattern_str, PathSpec) — un spec par pattern non
    négatif. Sert à identifier QUEL pattern a causé l'exclusion d'un fichier
    (pathspec ne fournit pas cette info via match_file).

    Les patterns de négation (`!…`) sont ignorés ici : ils ne causent pas
    d'exclusion, ils l'annulent.
    """
    if not _PATHSPEC_OK:
        return []
    if patterns is None:
        patterns = load_ignore_patterns()
    out = []
    for p in patterns:
        if p.startswith("!"):
            continue
        try:
            out.append((p, pathspec.PathSpec.from_lines("gitwildmatch", [p])))
        except Exception:
            pass
    return out


def ignore_which(individual_specs, rel_path: str, is_dir: bool) -> str:
    """Retourne le premier pattern qui matche `rel_path`, ou "" si aucun.
    `individual_specs` provient de `compile_individual_specs()`.
    """
    candidate = rel_path + "/" if is_dir else rel_path
    for pat, sp in individual_specs:
        try:
            if sp.match_file(candidate):
                return pat
        except Exception:
            continue
    return ""


def load_ignore_patterns() -> List[str]:  # noqa: F811  (override avec doc enrichie)
    """Charge les patterns actifs depuis .zimaignore (avec création si absent).

    NOTE : pour le matching, utiliser `compile_ignore_spec()` qui retourne un
    objet PathSpec optimisé.
    """
    return parse_ignore_lines(read_ignore_text())


def ensure_dirs():
    for d in [APP_DATA_ROOT, REPORTS_DIR, TEMP_DIR]:
        d.mkdir(parents=True, exist_ok=True)


def validate_path(p: str) -> bool:
    return any(p.startswith(pfx) for pfx in VALID_PREFIXES)


def path_exists(p: str) -> dict:
    if not validate_path(p):
        return {"path": p, "exists": False, "is_dir": False, "valid_prefix": False, "readable": False}
    pp = Path(p)
    try:
        exists = pp.exists()
        is_dir = pp.is_dir() if exists else False
        readable = os.access(p, os.R_OK) if exists else False
    except Exception:
        exists = is_dir = readable = False
    out = {"path": p, "exists": exists, "is_dir": is_dir, "valid_prefix": True, "readable": readable}
    # v3.11 : informations disque attachées (peut être None si timeout/erreur)
    if exists and is_dir:
        out["disk"] = disk_info(p)
    else:
        out["disk"] = None
    return out


# ── v3.11 — Espace disque avec cache TTL et timeout sur les chemins réseau ───
import shutil
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
try:
    import psutil  # déjà dans requirements.txt
    _PSUTIL_OK = True
except Exception:
    _PSUTIL_OK = False

# Cache : {path: (timestamp, payload)}.
_disk_cache: dict = {}
_disk_cache_lock = threading.Lock()
_disk_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="diskinfo")

# TTL différenciés : on revérifie souvent en local (cheap),
# moins souvent en réseau (peut être lent et inutile).
_DISK_TTL_LOCAL_S    = 10
_DISK_TTL_NETWORK_S  = 30
# Timeout par appel — au-delà on retourne stale=True avec disk:null.
_DISK_TIMEOUT_LOCAL_S    = 1.0
_DISK_TIMEOUT_NETWORK_S  = 3.0
# DRY F19 : source unique des seuils SMART (ans). Refs : main.py (export), selfcheck.py (verdict).
SMART_WATCH_YEARS = 3   # >= : surveillance (info, status ok)
SMART_OLD_YEARS   = 5   # >= : warn (disque ancien)

# A1bis : seuils d'observabilite systeme (warn/crit). Editables au fichier.
HEALTH_THRESHOLDS = {
    "cpu_warn": 85.0, "cpu_crit": 97.0,
    "mem_warn": 85.0, "mem_crit": 95.0,
    "load_per_cpu_warn": 2.0, "load_per_cpu_crit": 4.0,
    "disk_free_pct_warn": 10.0, "disk_free_pct_crit": 5.0,
}


def _disk_usage_raw(p: str) -> dict:
    """Appel direct à shutil.disk_usage + psutil pour le mount/fstype.
    Bloquant — à appeler dans un thread avec timeout côté caller."""
    usage = shutil.disk_usage(p)  # bloquant possible sur NFS/SMB
    mount = ""
    fstype = ""
    if _PSUTIL_OK:
        # Trouver la partition qui contient ce path : on cherche le mountpoint
        # le plus long qui est un préfixe du chemin réel.
        try:
            real = os.path.realpath(p)
            best = None
            for part in psutil.disk_partitions(all=True):
                mp = part.mountpoint
                if real == mp or real.startswith(mp.rstrip("/") + "/"):
                    if best is None or len(mp) > len(best.mountpoint):
                        best = part
            if best is not None:
                mount = best.mountpoint
                fstype = best.fstype
        except Exception:
            pass
    # used_pct = "% rempli du point de vue user" — cohérent avec `df -h`.
    # Formule : used / (used + free) où free = espace dispo utilisateur.
    # Sur ext4 la réserve root est exclue, donc (total - free) / total
    # surestime le remplissage.
    denom_user = usage.used + usage.free
    used_pct = round(usage.used / denom_user * 100, 1) if denom_user else None
    return {
        "total_bytes": int(usage.total),
        "free_bytes":  int(usage.free),
        "used_bytes":  int(usage.used),
        "used_pct":    used_pct,
        "mount_point": mount,
        "fstype":      fstype,
    }


def disk_info(p: str) -> dict | None:
    """Retourne les infos disque pour `p`, avec cache TTL et timeout.

    En cas de timeout (réseau lent) ou d'erreur, retourne un dict avec
    `error` renseigné et les champs de capacité à None.
    Retourne None uniquement si le chemin est invalide.
    """
    if not p or not validate_path(p):
        return None

    is_network = p.startswith("/network/")
    ttl = _DISK_TTL_NETWORK_S if is_network else _DISK_TTL_LOCAL_S
    timeout = _DISK_TIMEOUT_NETWORK_S if is_network else _DISK_TIMEOUT_LOCAL_S

    now = time.time()
    # Vérification du cache.
    with _disk_cache_lock:
        cached = _disk_cache.get(p)
        if cached and (now - cached[0]) < ttl:
            return cached[1]

    # Calcul réel dans un thread avec timeout.
    payload: dict = {
        "total_bytes": None, "free_bytes": None, "used_bytes": None,
        "used_pct": None, "mount_point": "", "fstype": "",
        "is_network": is_network, "error": None, "stale": False,
    }
    try:
        fut = _disk_executor.submit(_disk_usage_raw, p)
        data = fut.result(timeout=timeout)
        payload.update(data)
    except FuturesTimeout:
        payload["error"] = "timeout"
        payload["stale"] = True
        # On garde l'ancienne valeur en cache si dispo, en la marquant stale.
        with _disk_cache_lock:
            old = _disk_cache.get(p)
        if old:
            stale_payload = dict(old[1])
            stale_payload["stale"] = True
            stale_payload["error"] = "timeout"
            return stale_payload
    except Exception as e:
        payload["error"] = str(e)[:120]

    with _disk_cache_lock:
        _disk_cache[p] = (now, payload)
    return payload


_history_lock = threading.Lock()


def load_paths_history() -> List[dict]:
    if not PATHS_HISTORY_FILE.exists():
        return []
    try:
        with open(PATHS_HISTORY_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def add_paths_to_history(source: str, target: str):
    if not source or not target:
        return
    with _history_lock:
        history = load_paths_history()
        history = [e for e in history if not (e.get("source") == source and e.get("target") == target)]
        history.insert(0, {"source": source, "target": target})
        history = history[:HISTORY_MAX]
        try:
            APP_DATA_ROOT.mkdir(parents=True, exist_ok=True)
            tmp = PATHS_HISTORY_FILE.with_suffix(".tmp")
            with open(tmp, "w") as f:
                json.dump(history, f, indent=2, ensure_ascii=False)
            tmp.replace(PATHS_HISTORY_FILE)
        except Exception:
            pass


# -- F8 -- Profils de synchro enregistres (source -> cible + methode) --
PROFILES_FILE = APP_DATA_ROOT / "sync_profiles.json"
PROFILES_MAX = 50
_profiles_lock = threading.Lock()


def _write_profiles(profiles: List[dict]):
    try:
        APP_DATA_ROOT.mkdir(parents=True, exist_ok=True)
        tmp = PROFILES_FILE.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(profiles, f, indent=2, ensure_ascii=False)
        tmp.replace(PROFILES_FILE)
    except Exception:
        pass


def load_profiles() -> List[dict]:
    if not PROFILES_FILE.exists():
        return []
    try:
        with open(PROFILES_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_profile(name: str, source: str, target: str, method: str = "fast") -> List[dict]:
    """Upsert d'un profil par nom (remplace si meme nom). Retourne la liste a jour."""
    name = (name or "").strip()
    if not name or not source or not target:
        return load_profiles()
    with _profiles_lock:
        profiles = [p for p in load_profiles() if p.get("name") != name]
        profiles.insert(0, {"name": name, "source": source,
                            "target": target, "method": method or "fast"})
        profiles = profiles[:PROFILES_MAX]
        _write_profiles(profiles)
        return profiles


def delete_profile(name: str) -> List[dict]:
    name = (name or "").strip()
    with _profiles_lock:
        profiles = [p for p in load_profiles() if p.get("name") != name]
        _write_profiles(profiles)
        return profiles


log_buffer: List[str] = []
_log_lock = threading.Lock()


class _MemoryHandler(logging.Handler):
    def emit(self, record: logging.LogRecord):
        msg = self.format(record)
        with _log_lock:
            log_buffer.append(msg)
            if len(log_buffer) > 2000:
                log_buffer.pop(0)


LOG_LEVELS = {
    "DEBUG": logging.DEBUG, "INFO": logging.INFO, "WARNING": logging.WARNING,
    "ERROR": logging.ERROR, "CRITICAL": logging.CRITICAL,
}
LOG_LEVEL_FILE = APP_DATA_ROOT / "log_level"


def _read_persisted_log_level() -> str:
    """Niveau console persiste (str) ou 'INFO' par defaut."""
    try:
        v = LOG_LEVEL_FILE.read_text(encoding="utf-8").strip().upper()
        if v in LOG_LEVELS:
            return v
    except Exception:
        pass
    return "INFO"


def _console_handler():
    """Retrouve le StreamHandler console du logger 'zimacompare' (ou None)."""
    lg = logging.getLogger("zimacompare")
    for h in lg.handlers:
        if isinstance(h, logging.StreamHandler) and not isinstance(h, RotatingFileHandler):
            # _MemoryHandler n'est pas un StreamHandler -> seul le console matche.
            return h
    return None


def get_runtime_log_level() -> str:
    """Niveau courant du handler console (str)."""
    h = _console_handler()
    if h is not None:
        for name, lvl in LOG_LEVELS.items():
            if lvl == h.level:
                return name
    return _read_persisted_log_level()


def set_runtime_log_level(name: str) -> str:
    """Ajuste le niveau du handler console A CHAUD + persiste. Retourne le
    niveau applique. Le fichier et le buffer memoire restent a DEBUG (trace
    complete conservee) ; seul l'affichage console est filtre."""
    name = (name or "").strip().upper()
    if name not in LOG_LEVELS:
        raise ValueError(f"Niveau inconnu : {name!r} (valides : {list(LOG_LEVELS)})")
    h = _console_handler()
    if h is not None:
        h.setLevel(LOG_LEVELS[name])
    try:
        LOG_LEVEL_FILE.write_text(name, encoding="utf-8")
    except Exception:
        pass
    return name


def setup_logging() -> logging.Logger:
    ensure_dirs()
    logger = logging.getLogger("zimacompare")
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s [%(levelname)-5s] %(message)s", datefmt="%H:%M:%S")

    fh = RotatingFileHandler(LOG_FILE, maxBytes=20 * 1024 * 1024, backupCount=5, encoding="utf-8")
    fh.setLevel(logging.DEBUG); fh.setFormatter(fmt); logger.addHandler(fh)

    mh = _MemoryHandler()
    mh.setLevel(logging.DEBUG); mh.setFormatter(fmt); logger.addHandler(mh)

    sh = logging.StreamHandler()
    sh.setLevel(LOG_LEVELS.get(_read_persisted_log_level(), logging.INFO))
    sh.setFormatter(fmt); logger.addHandler(sh)

    return logger
