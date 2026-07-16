# -*- coding: utf-8 -*-
"""
================================================================================
 BluOS Artwork Scanner — module backend (ZimaCompare&Tag v16+)
================================================================================
Transformation du script CLI `bluos_artwork_scanner.py` en module backend.
- Volet A (réseau) : scan_network(ip, port, timeout) -> {player, results}
- Volet B (fichiers) : diagnose_library(source_path) -> results

Tous les seuils/params sont lus via bluos_config (Lot 1) :
  get_bluos_param(key, default). Editables dans l'UI.

Règles officielles BluOS : support.bluos.net/hc/en-us/articles/360000368827
  - Ordre pochette dossier : folder.jpg > cover.jpg > folder.png > cover.png,
    sinon image JPEG/PNG intégrée. .bmp jamais reconnu.
  - Externe : "Optimiser" ON -> 600 Ko-4 Mo redim. auto 600x600 ; >=4 Mo non traité.
              "Optimiser" OFF -> < 1200x1200 px ET < 600 Ko.
  - Intégrée : JPEG/PNG uniquement, < 600 Ko, même image sur toutes les pistes.
"""

import base64
import hashlib
import io
import os
import re
import xml.etree.ElementTree as ET
from urllib.parse import quote

# ----------------------------------------------------------------------------
# Logger backend (fallback print si import indisponible hors app)
# ----------------------------------------------------------------------------
try:
    from tagaudit.core.logger import logger as _LOG

    def _default_log(msg):
        _LOG.info(msg)
except Exception:  # pragma: no cover - fallback CLI/tests
    def _default_log(msg):
        print(msg)

# ----------------------------------------------------------------------------
# Accès aux paramètres éditables (Lot 1 : table bluos_config)
# ----------------------------------------------------------------------------
try:
    from tagaudit.core.audit_registry import get_bluos_param
except Exception:  # pragma: no cover
    def get_bluos_param(key, default=None, db_path=None):
        return default

# ----------------------------------------------------------------------------
# Dépendances optionnelles (dégradation gracieuse)
# ----------------------------------------------------------------------------
try:
    import requests
    HAS_REQUESTS = True
except ImportError:  # pragma: no cover
    HAS_REQUESTS = False

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

try:
    import mutagen  # noqa: F401
    HAS_MUTAGEN = True
except ImportError:
    HAS_MUTAGEN = False

# ----------------------------------------------------------------------------
# Constantes fixes (non éditables : liste d'extensions reconnues par BluOS)
# ----------------------------------------------------------------------------
VALID_EXTERNAL_NAMES = {
    "folder.jpg", "folder.jpeg", "cover.jpg", "cover.jpeg",
    "folder.png", "cover.png",
}
AUDIO_EXTENSIONS = {
    ".flac", ".mp3", ".m4a", ".mp4", ".aac", ".alac",
    ".ogg", ".opus", ".wav", ".aiff", ".aif", ".wma",
}


# ----------------------------------------------------------------------------
# Helpers : chargement des paramètres éditables depuis bluos_config
# ----------------------------------------------------------------------------
def _load_params():
    """Lit les seuils BluOS depuis bluos_config (Lot 1) avec casts + défauts."""
    def _int(key, dflt):
        try:
            return int(float(get_bluos_param(key, dflt)))
        except (TypeError, ValueError):
            return dflt
    return {
        "embedded_max_bytes": _int("embedded_max_kb", 600) * 1024,
        "external_autoresize_min": _int("external_autoresize_min_kb", 600) * 1024,
        "external_autoresize_max": _int("external_autoresize_max_kb", 4096) * 1024,
        "external_no_optimize_max_px": _int("external_no_optimize_max_px", 1200),
        "placeholder_max_bytes": _int("placeholder_max_kb", 50) * 1024,
        "placeholder_min_count": _int("placeholder_min_count", 4),
    }


# ============================================================================
# PARTIE 1 — Scan réseau via l'API locale BluOS
# ============================================================================
class BluOSClient:
    """Petit client pour l'API locale BluOS (port 11000, HTTP, XML)."""

    def __init__(self, ip, port=11000, timeout=10):
        self.base = f"http://{ip}:{port}"
        self.timeout = timeout
        self.session = requests.Session()

    def _get(self, path):
        r = self.session.get(f"{self.base}{path}", timeout=self.timeout)
        r.raise_for_status()
        return r

    def check_reachable(self):
        """Vérifie que le lecteur répond bien à /SyncStatus (nom, modèle)."""
        r = self._get("/SyncStatus")
        root = ET.fromstring(r.content)
        return {
            "name": root.get("name", "?"),
            "model": root.get("modelName", "?"),
        }

    def browse(self, key=None):
        path = "/Browse"
        if key:
            path += f"?key={quote(key, safe='')}"
        r = self._get(path)
        return ET.fromstring(r.content)

    def fetch_image(self, image_url):
        if image_url.startswith("http://") or image_url.startswith("https://"):
            url = image_url
        else:
            sep = "&" if "?" in image_url else "?"
            url = f"{self.base}{image_url}{sep}followRedirects=1"
        r = self.session.get(url, timeout=self.timeout)
        r.raise_for_status()
        return r.content, r.headers.get("Content-Type", "")


def find_albums_browse_key(client, log=_default_log, debug=False):
    """Retrouve l'entrée 'bibliothèque locale' puis la vue 'Albums'."""
    root = client.browse()
    if debug:
        items = root.findall("item")
        log("  [debug] menu racine : {} éléments : ".format(len(items))
            + ", ".join(f"'{it.get('text')}'->{it.get('browseKey')}" for it in items))

    local_key = None
    for item in root.findall("item"):
        bk = item.get("browseKey", "")
        if bk.startswith("LocalMusic"):
            local_key = bk
            break
    if not local_key:
        raise RuntimeError(
            "Impossible de trouver la bibliothèque locale (LocalMusic) sur ce "
            "lecteur. Vérifiez qu'un dossier réseau ou une clé USB est bien "
            "configuré comme source musicale dans BluOS Controller."
        )

    lib = client.browse(local_key)
    lib_items = lib.findall("item")
    if debug:
        log("  [debug] bibliothèque locale : {} éléments : ".format(len(lib_items))
            + ", ".join(f"'{it.get('text')}'->{it.get('browseKey')}" for it in lib_items))

    candidates = []
    for item in lib_items:
        text = (item.get("text") or "").strip().lower()
        bk = item.get("browseKey")
        if bk and "album" in text:
            candidates.append((text == "albums", bk, item.get("text")))
    if not candidates:
        raise RuntimeError(
            "Impossible de trouver la vue 'Albums' de la bibliothèque locale."
        )
    candidates.sort(key=lambda c: not c[0])  # correspondance exacte en premier
    chosen = candidates[0]
    log(f"  Vue albums trouvée : '{chosen[2]}'")
    return chosen[1]


def _summarize_types(items):
    counts = {}
    for it in items:
        t = it.get("type") or "?"
        counts[t] = counts.get(t, 0) + 1
    return ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))


def collect_local_albums(client, albums_key, log=_default_log, debug=False, max_depth=8):
    """Parcourt récursivement la vue Albums de la bibliothèque locale."""
    albums = []
    seen_albums = set()
    visited_keys = set()
    calls = [0]

    def walk(key, depth):
        if depth > max_depth or key in visited_keys:
            return
        visited_keys.add(key)
        page_key = key
        page = 0
        while page_key:
            page += 1
            calls[0] += 1
            browse = client.browse(page_key)
            items = browse.findall("item")

            if debug and depth <= 2:
                log(f"  [debug] profondeur {depth}, clé='{page_key[:60]}', "
                    f"{len(items)} éléments : {_summarize_types(items)}")

            for it in items:
                t = it.get("type")
                bk = it.get("browseKey")
                if t == "album":
                    uid = (it.get("text"), it.get("text2"), it.get("image"))
                    if uid not in seen_albums:
                        seen_albums.add(uid)
                        albums.append({
                            "title": it.get("text") or "(sans titre)",
                            "artist": it.get("text2") or "",
                            "image": it.get("image"),
                        })
                elif t == "track":
                    continue
                elif bk:
                    walk(bk, depth + 1)

            if len(albums) and len(albums) % 100 < 5:
                log(f"  ... {len(albums)} albums trouvés jusqu'ici "
                    f"({calls[0]} requêtes envoyées)")

            page_key = browse.get("nextKey")

    walk(albums_key, 0)
    log(f"  ... terminé : {len(albums)} albums, {calls[0]} requêtes au lecteur.")
    return albums


def analyze_network_artwork(client, albums, params, log=_default_log, stop_event=None,
                            progress_cb=None):
    """Télécharge la pochette de chaque album et repère celles qui posent
    vraiment problème (petite taille + répétée = icône générique BluOS).

    params : dict issu de _load_params() (placeholder_max_bytes, placeholder_min_count).
    stop_event : threading.Event optionnel pour interruption (Lot 3).
    progress_cb : callable(idx, total) optionnel pour publier la progression (Lot 3).
    """
    placeholder_max_bytes = params["placeholder_max_bytes"]
    placeholder_min_count = params["placeholder_min_count"]

    results = []
    small_clusters = {}

    total = len(albums)
    for idx, alb in enumerate(albums, start=1):
        if stop_event is not None and stop_event.is_set():
            log("  ... scan réseau interrompu par l'utilisateur.")
            break
        if idx % 50 == 0 or idx == total:
            log(f"  ... vérification des pochettes {idx}/{total}")
        if progress_cb is not None:
            progress_cb(idx, total)

        entry = {
            "artist": alb["artist"],
            "title": alb["title"],
            "status": "ok",
            "detail": "",
        }
        if not alb["image"]:
            entry["status"] = "missing"
            entry["detail"] = "Le lecteur ne renvoie aucune image pour cet album."
        else:
            try:
                data, ctype = client.fetch_image(alb["image"])
                if not data or "image" not in ctype:
                    entry["status"] = "missing"
                    entry["detail"] = f"Réponse invalide du lecteur ({ctype or 'vide'})."
                else:
                    size = len(data)
                    entry["size"] = size
                    if size < placeholder_max_bytes:
                        h = hashlib.md5(data).hexdigest()
                        cluster = small_clusters.setdefault(h, {
                            "count": 0, "size": size, "data": data, "ctype": ctype, "idxs": []
                        })
                        cluster["count"] += 1
                        cluster["idxs"].append(len(results))
            except Exception as e:
                entry["status"] = "error"
                entry["detail"] = f"Erreur réseau : {e}"
        results.append(entry)

    flagged_thumb_count = 0
    for h, cluster in small_clusters.items():
        if cluster["count"] >= placeholder_min_count:
            thumb_uri = "data:" + (cluster["ctype"] or "image/jpeg") + ";base64," + \
                base64.b64encode(cluster["data"]).decode("ascii")
            flagged_thumb_count += 1
            for i in cluster["idxs"]:
                results[i]["status"] = "placeholder"
                results[i]["thumb"] = thumb_uri
                results[i]["detail"] = (
                    f"Image de {cluster['size'] / 1024:.0f} Ko, identique à "
                    f"{cluster['count'] - 1} autres albums : taille et répétition "
                    f"typiques de l'icône générique BluOS. Vérifiez la miniature "
                    f"ci-contre — si c'est une vraie pochette (coffret/série), "
                    f"ignorez cette ligne."
                )

    log(f"  ... {flagged_thumb_count} image(s) suspecte(s) identifiée(s) "
        f"(petite taille + répétée).")
    return results


# ============================================================================
# PARTIE 2 — Scan des fichiers audio locaux
# ============================================================================
def _extract_embedded_bytes(filepath):
    """Octets bruts de la première pochette intégrée, ou None."""
    if not HAS_MUTAGEN:
        return None

    from mutagen import File as MutagenFile

    ext = os.path.splitext(filepath)[1].lower()
    try:
        if ext == ".flac":
            from mutagen.flac import FLAC
            audio = FLAC(filepath)
            if audio.pictures:
                return audio.pictures[0].data

        elif ext in (".mp3", ".wav", ".aiff", ".aif"):
            from mutagen.id3 import ID3
            try:
                tags = ID3(filepath)
            except Exception:
                mf = MutagenFile(filepath)
                tags = getattr(mf, "tags", None)
            if tags and hasattr(tags, "getall"):
                apics = tags.getall("APIC")
                if apics:
                    return apics[0].data

        elif ext in (".m4a", ".mp4", ".aac", ".alac"):
            from mutagen.mp4 import MP4
            audio = MP4(filepath)
            covr = audio.tags.get("covr") if audio.tags else None
            if covr:
                return bytes(covr[0])

        elif ext in (".ogg", ".opus"):
            mf = MutagenFile(filepath)
            pics = mf.get("metadata_block_picture") if mf else None
            if pics:
                from mutagen.flac import Picture
                raw = base64.b64decode(pics[0])
                return Picture(raw).data

        else:
            mf = MutagenFile(filepath)
            if mf and mf.tags:
                for key in list(mf.tags.keys()):
                    if "APIC" in key or "covr" in key.lower():
                        val = mf.tags[key]
                        if isinstance(val, list):
                            val = val[0]
                        data = getattr(val, "data", None)
                        if data is None and not isinstance(val, str):
                            try:
                                data = bytes(val)
                            except Exception:
                                data = None
                        if data:
                            return data
    except Exception:
        return None
    return None


def _identify_image(data):
    """Renvoie (format, largeur, hauteur) d'une image à partir de ses octets."""
    if not HAS_PIL:
        return ("inconnu", None, None)
    try:
        img = Image.open(io.BytesIO(data))
        return (img.format or "inconnu", img.width, img.height)
    except Exception:
        return ("illisible", None, None)


def scan_local_folders(root_path, params, log=_default_log, stop_event=None,
                       progress_cb=None):
    """Parcourt root_path ; chaque dossier avec de l'audio est diagnostiqué.

    params : dict issu de _load_params() (seuils externes/embedded).
    """
    embedded_max_bytes = params["embedded_max_bytes"]
    external_autoresize_min = params["external_autoresize_min"]
    external_autoresize_max = params["external_autoresize_max"]
    external_no_optimize_max_px = params["external_no_optimize_max_px"]

    results = []
    folder_count = 0

    for dirpath, _dirnames, filenames in os.walk(root_path):
        if stop_event is not None and stop_event.is_set():
            log("  ... scan fichiers interrompu par l'utilisateur.")
            break
        audio_files = [f for f in filenames if os.path.splitext(f)[1].lower() in AUDIO_EXTENSIONS]
        if not audio_files:
            continue

        folder_count += 1
        if folder_count % 25 == 0:
            log(f"  ... {folder_count} dossiers d'album analysés")
        if progress_cb is not None:
            progress_cb(folder_count, None)

        entry = {
            "folder": dirpath,
            "issues": [],
            "notes": [],
            "tracks_checked": len(audio_files),
        }

        lower_files = {f.lower(): f for f in filenames}

        for candidate in ("folder.bmp", "cover.bmp"):
            if candidate in lower_files:
                real = lower_files[candidate]
                entry["issues"].append(
                    f"« {real} » est au format BMP : BluOS ne recherche que les "
                    f"extensions .jpg/.jpeg/.png pour la pochette de dossier, ce "
                    f"fichier est donc totalement ignoré par l'indexation."
                )

        external_found = None
        for name in VALID_EXTERNAL_NAMES:
            if name in lower_files:
                external_found = lower_files[name]
                break

        if external_found:
            fp = os.path.join(dirpath, external_found)
            try:
                size = os.path.getsize(fp)
            except OSError:
                size = None
            if size is not None:
                entry["external_art_file"] = external_found
                entry["external_art_size"] = size
                if size >= external_autoresize_max:
                    entry["issues"].append(
                        f"« {external_found} » pèse {size / 1024 / 1024:.1f} Mo (≥ 4 Mo) : "
                        f"trop lourd pour être traité par BluOS, même avec "
                        f"« Optimiser les pochettes » activé."
                    )
                elif size >= external_autoresize_min:
                    entry["notes"].append(
                        f"« {external_found} » pèse {size / 1024:.0f} Ko (entre 600 Ko et 4 Mo) : "
                        f"BluOS le redimensionnera automatiquement SI l'option "
                        f"« Optimiser les pochettes » est activée. Si elle est "
                        f"désactivée, cette pochette ne s'affichera pas."
                    )
                elif HAS_PIL:
                    try:
                        with open(fp, "rb") as fh:
                            fmt, w, h = _identify_image(fh.read())
                        if w and h and (w > external_no_optimize_max_px or h > external_no_optimize_max_px):
                            entry["notes"].append(
                                f"« {external_found} » fait {w}x{h}px : si « Optimiser les "
                                f"pochettes » est désactivée dans BluOS, la résolution doit "
                                f"rester sous 1200x1200px, sinon cette pochette ne s'affichera pas."
                            )
                    except Exception:
                        pass

        embedded_hashes = {}
        reported = set()
        if HAS_MUTAGEN:
            for f in audio_files:
                fp = os.path.join(dirpath, f)
                data = _extract_embedded_bytes(fp)
                if not data:
                    continue
                fmt, w, h = _identify_image(data)
                embedded_hashes[f] = hashlib.md5(data).hexdigest()

                if fmt and fmt.upper() not in ("JPEG", "PNG") and "format" not in reported:
                    entry["issues"].append(
                        f"Pochette intégrée au format {fmt} détectée (ex. « {f} ») : "
                        f"BluOS n'accepte que le JPEG et le PNG en pochette intégrée "
                        f"— un BMP, même petit, ne sera jamais indexé."
                    )
                    reported.add("format")

                if len(data) >= embedded_max_bytes and "size" not in reported:
                    entry["issues"].append(
                        f"Pochette intégrée de {len(data) / 1024:.0f} Ko (ex. « {f} ») : "
                        f"au-delà de 600 Ko, BluOS n'indexe pas la pochette intégrée, "
                        f"quel que soit le réglage « Optimiser les pochettes »."
                    )
                    reported.add("size")
        elif audio_files and not external_found:
            entry["notes"].append(
                "Le module 'mutagen' n'est pas installé : impossible d'inspecter les "
                "pochettes intégrées dans les fichiers audio de ce dossier."
            )

        distinct_hashes = set(embedded_hashes.values())
        if len(distinct_hashes) > 1:
            entry["issues"].append(
                f"Les pochettes intégrées diffèrent d'une piste à l'autre "
                f"({len(distinct_hashes)} images différentes trouvées parmi "
                f"{len(embedded_hashes)} pistes analysées) : BluOS attend la "
                f"même image sur toutes les pistes d'un album."
            )
        elif not embedded_hashes and not external_found and HAS_MUTAGEN:
            entry["issues"].append(
                "Aucune pochette trouvée : ni fichier folder.jpg/cover.jpg dans "
                "le dossier, ni pochette intégrée dans les fichiers audio."
            )

        if entry["issues"] or entry["notes"]:
            results.append(entry)

    return results


# ============================================================================
# Rapprochement des deux volets (best effort, par nom d'artiste/album)
# ============================================================================
def _normalize(text):
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def cross_reference(network_results, folder_results):
    """Marque les dossiers locaux correspondant à un album fautif réseau."""
    flagged = [r for r in network_results if r["status"] in ("missing", "placeholder", "error")]
    flagged_norms = [
        (_normalize(r["artist"]) + _normalize(r["title"]), r)
        for r in flagged
    ]
    for folder_entry in folder_results:
        folder_norm = _normalize(os.path.basename(folder_entry["folder"])) + \
            _normalize(os.path.basename(os.path.dirname(folder_entry["folder"])))
        for norm, net_entry in flagged_norms:
            if norm and (norm in folder_norm or folder_norm in norm):
                folder_entry["matched_network_album"] = f"{net_entry['artist']} — {net_entry['title']}"
                break
    return folder_results


# ============================================================================
# API PUBLIQUE — fonctions pures pour le backend (Lot 2)
# ============================================================================
def scan_network(ip=None, port=None, timeout=None, log=_default_log,
                 stop_event=None, progress_cb=None, debug=False):
    """Volet A : scanne le Node BluOS et renvoie les albums fautifs.

    Params lus depuis bluos_config si non fournis. Renvoie :
      {"player": {name, model}, "results": [...], "flagged": int}
    Lève RuntimeError si le lecteur est injoignable ou la lib introuvable.
    """
    if not HAS_REQUESTS:
        raise RuntimeError("Le module 'requests' est requis pour le scan réseau BluOS.")

    if ip is None:
        ip = get_bluos_param("bluos_ip", "192.168.1.121")
    if port is None:
        try:
            port = int(float(get_bluos_param("bluos_port", 11000)))
        except (TypeError, ValueError):
            port = 11000
    if timeout is None:
        try:
            timeout = int(float(get_bluos_param("bluos_timeout", 10)))
        except (TypeError, ValueError):
            timeout = 10

    params = _load_params()
    log(f"Connexion à {ip}:{port} ...")
    client = BluOSClient(ip, port, timeout)
    player_info = client.check_reachable()
    log(f"Connecté : {player_info['name']} ({player_info['model']})")

    albums_key = find_albums_browse_key(client, log=log, debug=debug)
    albums = collect_local_albums(client, albums_key, log=log, debug=debug)
    log(f"{len(albums)} albums trouvés dans la bibliothèque locale.")

    results = analyze_network_artwork(client, albums, params, log=log,
                                      stop_event=stop_event, progress_cb=progress_cb)
    flagged = [r for r in results if r["status"] != "ok"]
    log(f"-> {len(flagged)} album(s) avec une pochette manquante ou générique.")
    return {"player": player_info, "results": results, "flagged": len(flagged)}


def _master_csv_default():
    """Chemin canonique du master_scan.csv (moteur tag), avec fallback."""
    p = "/app_data/tagaudit/data/master_scan.csv"
    try:
        import sys as _sys
        if "/app/tagaudit" not in _sys.path:
            _sys.path.insert(0, "/app/tagaudit")
        from core import config as _tagcfg  # type: ignore
        cand = getattr(_tagcfg, "master_csv_path", None)
        if cand:
            p = str(cand)
    except Exception:
        pass
    return p


def _norm_cover_format(fmt):
    """Normalise le format pochette (CSV = MIME 'image/jpeg') vers JPEG/PNG/BMP..."""
    if not fmt:
        return ""
    f = fmt.strip().lower()
    if "/" in f:
        f = f.split("/", 1)[1]  # image/jpeg -> jpeg
    f = f.replace("jpg", "jpeg")
    return f.upper()  # JPEG / PNG / BMP / ...


def _win_path(linux_path):
    """Convertit un chemin Linux -> Windows a l'IDENTIQUE du rapport d'audit ZimaTag.
    Reutilise ExcelExporter._translate_path_for_windows (meme source que html_export._win).
    Fallback : simple remplacement des separateurs si l'import echoue.
    """
    try:
        from tagaudit.export.excel_export import ExcelExporter
        return ExcelExporter._translate_path_for_windows(str(linux_path))
    except Exception:
        return str(linux_path or "").replace("/", "\\")


def diagnose_from_csv(csv_path=None, network_results=None, params=None,
                      log=_default_log, stop_event=None, progress_cb=None):
    """Volet B optimisé : diagnostic depuis la table SQLite `tracks`
    (master_scan.db, embedded déjà scanné) + check FS léger (os.listdir)
    pour les pochettes externes.

    Renvoie la même structure que scan_local_folders. Ne re-scanne pas les
    fichiers audio (pas de mutagen) : lit les colonnes cover_* de `tracks`.

    [LOT v20-5a] Lit désormais master_scan.db via core.db, plus
    master_scan.csv. Le paramètre csv_path est conservé pour compatibilité
    de signature mais n'est plus exploité (source SQLite unique désormais) --
    aucun appelant ne le passe avec une valeur non-None dans ce dépôt
    (vérifié par grep).
    """
    import sys as _sys
    if "/app/tagaudit" not in _sys.path:
        _sys.path.insert(0, "/app/tagaudit")
    from core import db

    if params is None:
        params = _load_params()
    embedded_max_bytes = params["embedded_max_bytes"]
    external_autoresize_min = params["external_autoresize_min"]
    external_autoresize_max = params["external_autoresize_max"]

    if not os.path.isfile(db.DB_PATH):
        raise RuntimeError(f"master_scan.db introuvable : {db.DB_PATH!r}")

    conn = db.connect()
    try:
        sql_rows = conn.execute(
            "SELECT directory, has_cover, cover_format, cover_size, cover_md5, "
            "cover_width, cover_height, cover_count FROM tracks ORDER BY id"
        ).fetchall()
    finally:
        conn.close()

    # 1) Regrouper les lignes par dossier (directory) -- variable "sql_rows"
    # distincte de "rows" (nom deja utilise plus bas pour la sous-liste par
    # dossier, for dirpath, rows in by_dir.items()) : evite toute confusion.
    by_dir = {}
    for row in sql_rows:
        d = row["directory"] or ""
        if not d:
            continue
        by_dir.setdefault(d, []).append(row)

    log(f"  ... {len(by_dir)} dossier(s) dans master_scan.db")
    results = []
    done = 0
    for dirpath, rows in by_dir.items():
        if stop_event is not None and stop_event.is_set():
            log("  ... diagnostic SQL interrompu par l'utilisateur.")
            break
        done += 1
        if progress_cb is not None and done % 25 == 0:
            progress_cb(done, len(by_dir))

        entry = {"folder": dirpath, "win_path": _win_path(dirpath),
                 "issues": [], "notes": [], "tracks_checked": len(rows),
                 "cover_format": "", "cover_size": 0, "cover_width": 0,
                 "cover_height": 0, "cover_count": 0, "distinct_covers": 0}

        # -- Embedded (depuis SQLite) --
        md5s = set()
        fmt_reported = False
        size_reported = False
        for row in rows:
            has = (row["has_cover"] or "").strip().lower()
            if has not in ("yes", "true", "1"):
                continue
            cfmt = _norm_cover_format(row["cover_format"])
            try:
                csize = int(float(row["cover_size"] or 0))
            except (TypeError, ValueError):
                csize = 0
            cmd5 = (row["cover_md5"] or "").strip()
            if cmd5:
                md5s.add(cmd5)
            # Metadonnees pochette (1re piste avec cover fait foi pour l'affichage)
            if not entry["cover_format"] and cfmt:
                entry["cover_format"] = cfmt
                entry["cover_size"] = csize
                try:
                    entry["cover_width"] = int(float(row["cover_width"] or 0))
                    entry["cover_height"] = int(float(row["cover_height"] or 0))
                except (TypeError, ValueError):
                    pass
                try:
                    entry["cover_count"] = int(float(row["cover_count"] or 0))
                except (TypeError, ValueError):
                    pass
            if cfmt and cfmt not in ("JPEG", "PNG") and not fmt_reported:
                entry["issues"].append(
                    f"Pochette intégrée au format {cfmt} détectée : BluOS n'accepte "
                    f"que le JPEG et le PNG en pochette intégrée."
                )
                fmt_reported = True
            if csize >= embedded_max_bytes and not size_reported:
                entry["issues"].append(
                    f"Pochette intégrée de {csize / 1024:.0f} Ko : au-delà de 600 Ko, "
                    f"BluOS n'indexe pas la pochette intégrée."
                )
                size_reported = True
        entry["distinct_covers"] = len(md5s)
        if len(md5s) > 1:
            entry["issues"].append(
                f"Les pochettes intégrées diffèrent d'une piste à l'autre "
                f"({len(md5s)} images différentes) : BluOS attend la même image "
                f"sur toutes les pistes d'un album."
            )

        # -- Externe (check FS léger : os.listdir une fois, pas de mutagen) --
        external_found = None
        try:
            names = os.listdir(dirpath)
        except OSError:
            names = []
        lower_files = {n.lower(): n for n in names}
        for candidate in ("folder.bmp", "cover.bmp"):
            if candidate in lower_files:
                entry["issues"].append(
                    f"« {lower_files[candidate]} » est au format BMP : BluOS ne "
                    f"recherche que .jpg/.jpeg/.png pour la pochette de dossier."
                )
        for name in VALID_EXTERNAL_NAMES:
            if name in lower_files:
                external_found = lower_files[name]
                break
        if external_found:
            fp = os.path.join(dirpath, external_found)
            try:
                size = os.path.getsize(fp)
            except OSError:
                size = None
            if size is not None:
                entry["external_art_file"] = external_found
                entry["external_art_size"] = size
                if size >= external_autoresize_max:
                    entry["issues"].append(
                        f"« {external_found} » pèse {size / 1024 / 1024:.1f} Mo (≥ 4 Mo) : "
                        f"trop lourd pour BluOS, même avec « Optimiser » activé."
                    )
                elif size >= external_autoresize_min:
                    entry["notes"].append(
                        f"« {external_found} » pèse {size / 1024:.0f} Ko (600 Ko-4 Mo) : "
                        f"redimensionné auto SI « Optimiser » activé, sinon non affiché."
                    )

        # -- Aucune pochette du tout --
        if not md5s and not external_found:
            entry["issues"].append(
                "Aucune pochette trouvée : ni folder.jpg/cover.jpg, ni pochette "
                "intégrée détectée dans le scan."
            )

        if entry["issues"] or entry["notes"]:
            results.append(entry)

    if network_results:
        results = cross_reference(network_results, results)
    log(f"  ... {len(results)} dossier(s) avec un diagnostic (via SQLite).")
    return results


def diagnose_library(source_path=None, network_results=None, log=_default_log,
                     stop_event=None, progress_cb=None, use_csv=None,
                     csv_path=None):
    """Volet B : diagnostique la bibliothèque. Renvoie la liste des dossiers
    avec un problème (issues/notes). Croise avec network_results si fourni.

    Lot 4 : bascule automatiquement sur master_scan.db s'il existe (rapide,
    embedded déjà scanné + check FS léger pour l'externe). Fallback os.walk
    si pas de base ou use_csv=False.

    [LOT v20-5a] csv_path/use_csv conservés pour compatibilité de signature
    (aucun appelant ne les surcharge dans ce dépôt, vérifié par grep) --
    la décision se fait désormais sur la présence de master_scan.db, pas
    du CSV.
    """
    import sys as _sys
    if "/app/tagaudit" not in _sys.path:
        _sys.path.insert(0, "/app/tagaudit")
    from core import db

    params = _load_params()
    db_ok = os.path.isfile(db.DB_PATH)

    # Décision SQLite vs os.walk
    if use_csv is None:
        use_csv = db_ok
    if use_csv and db_ok:
        log(f"Diagnostic via master_scan.db : {db.DB_PATH}")
        return diagnose_from_csv(network_results=network_results,
                                 params=params, log=log, stop_event=stop_event,
                                 progress_cb=progress_cb)

    # Fallback : scan filesystem complet (ancien comportement)
    if not source_path or not os.path.isdir(source_path):
        raise RuntimeError(f"Chemin introuvable et pas de base master_scan.db : {source_path!r}")
    log(f"Scan du dossier local '{source_path}' (os.walk, pas de base)...")
    folder_results = scan_local_folders(source_path, params, log=log,
                                        stop_event=stop_event, progress_cb=progress_cb)
    if network_results:
        folder_results = cross_reference(network_results, folder_results)
    log(f"-> {len(folder_results)} dossier(s) avec un diagnostic à examiner.")
    return folder_results


# ============================================================================
# MÉCANIQUE THREAD / PROGRESSION / INTERRUPTIBILITÉ (Lot 3)
# Calqué sur le motif tagscan.py : garde-fou busy via l'état partagé,
# thread daemon, stop_event, publication de la progression via update_state.
# ============================================================================
import threading
import time

try:
    from config import update_state, get_state, AppState
    _HAS_STATE = True
except Exception:  # pragma: no cover
    _HAS_STATE = False

_lock = threading.Lock()
_stop_event = threading.Event()
_thread = None
# résultats du dernier scan : {"player":..., "network":[...], "folders":[...], "done":bool, "error":str}
_results = {"player": None, "network": [], "folders": [], "done": False, "error": ""}
_meta = {"started_at": 0.0, "ended_at": 0.0, "phase": ""}


def _bluos_progress(idx, total):
    """Callback de progression : publie dans l'état partagé (best effort)."""
    if not _HAS_STATE:
        return
    if total:
        pct = int(idx * 100 / total) if total else 0
        update_state(progress=pct, processed=idx, total=total,
                     current_file=f"BluOS {idx}/{total}")
    else:
        update_state(processed=idx, current_file=f"BluOS dossier {idx}")


def _run_bluos(ip, port, timeout, source_path):
    global _results
    _results = {"player": None, "network": [], "folders": [], "done": False, "error": ""}
    _meta["started_at"] = time.time()
    _meta["ended_at"] = 0.0
    try:
        _meta["phase"] = "network"
        net = scan_network(ip=ip, port=port, timeout=timeout,
                           stop_event=_stop_event, progress_cb=_bluos_progress)
        _results["player"] = net["player"]
        _results["network"] = net["results"]

        if source_path and not _stop_event.is_set():
            _meta["phase"] = "library"
            folders = diagnose_library(source_path, network_results=net["results"],
                                       stop_event=_stop_event, progress_cb=_bluos_progress)
            _results["folders"] = folders

        _results["done"] = True
        if _HAS_STATE:
            update_state(app_state=AppState.IDLE, progress=100,
                         current_file="BluOS terminé", error="")
    except Exception as e:
        _results["error"] = str(e)
        _results["done"] = True
        if _HAS_STATE:
            update_state(app_state=AppState.ERROR, error="bluos: %s" % e)
    finally:
        _meta["ended_at"] = time.time()
        _meta["phase"] = ""


def start_bluos_scan(ip=None, port=None, timeout=None, source_path=None):
    """Démarre un scan BluOS en thread. Retourne True ou 'busy'.

    Garde-fou croisé : refuse si une autre opération (scan/sync/clean) tourne
    (cf. §6). Utilise l'état partagé AppState.
    """
    global _thread
    if _HAS_STATE:
        state = get_state()
        if state["app_state"] not in (AppState.IDLE, AppState.ERROR):
            return "busy"
    with _lock:
        if _thread is not None and _thread.is_alive():
            return "busy"
        _stop_event.clear()
        if _HAS_STATE:
            update_state(app_state=AppState.SCANNING, method="bluos", source="",
                         target="", error="", scan_done=False, progress=0,
                         processed=0, total=0, current_file="BluOS : connexion...")
        _thread = threading.Thread(
            target=_run_bluos, args=(ip, port, timeout, source_path), daemon=True)
        _thread.start()
    return True


def stop_bluos_scan():
    """Demande l'interruption du scan BluOS en cours. True si un scan tournait."""
    with _lock:
        alive = _thread is not None and _thread.is_alive()
    if alive:
        _stop_event.set()
        return True
    return False


def bluos_status():
    """État courant du scan BluOS (pour /api/bluos/status)."""
    with _lock:
        running = _thread is not None and _thread.is_alive()
    st = {}
    if _HAS_STATE:
        s = get_state()
        st = {"progress": s.get("progress", 0), "processed": s.get("processed", 0),
              "total": s.get("total", 0), "current_file": s.get("current_file", "")}
    st.update({"running": running, "phase": _meta.get("phase", ""),
               "done": _results.get("done", False), "error": _results.get("error", "")})
    return st


def bluos_results():
    """Résultats du dernier scan BluOS (pour /api/bluos/results)."""
    flagged = [r for r in _results.get("network", []) if r.get("status") != "ok"]
    return {
        "player": _results.get("player"),
        "flagged_count": len(flagged),
        "network": _results.get("network", []),
        "folders": _results.get("folders", []),
        "done": _results.get("done", False),
        "error": _results.get("error", ""),
    }
