"""compressor.py — cœur métier autonome de ZimaCover.

Aucune dépendance à l'application ZimaCompare principale (pas d'import de
config.py, tagscan.py, etc.) — ce module est 100% indépendant. Seule
dépendance externe : Pillow (compression) et mutagen (lecture/écriture des
tags audio), toutes deux déjà utilisées par ZimaCompare mais réinstallées
ici indépendamment (voir requirements.txt).
"""
import csv
import hashlib
import io
import json
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from PIL import Image
from mutagen.mp3 import MP3
from mutagen.flac import FLAC, Picture as FlacPicture
from mutagen.mp4 import MP4, MP4Cover
from mutagen.id3 import APIC

if "/app/tagaudit" not in sys.path:
    sys.path.insert(0, "/app/tagaudit")
from core import config as _tagcfg

DATA_DIR = Path(os.environ.get("COVER_DATA_DIR", "./data")).resolve()
RESULT_CSV = DATA_DIR / "cover_scan.csv"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Noms de colonnes possibles (insensible à la casse) pour le chemin du fichier
# et l'album — élargir cette liste si votre CSV utilise d'autres intitulés.
PATH_COL_CANDIDATES = ["path", "filepath", "file_path", "file"]
ALBUM_COL_CANDIDATES = ["album", "tag_album", "albumname"]


def _sniff_delimiter(sample: str) -> str:
    try:
        return csv.Sniffer().sniff(sample, delimiters=";,\t").delimiter
    except Exception:
        return ";" if sample.count(";") >= sample.count(",") else ","


def load_candidate_files(source: str) -> Tuple[List[dict], Optional[str]]:
    """
    Charge la liste des fichiers à traiter depuis master_scan.csv (info déjà
    connue de l'app principale) plutôt que de rescanner le disque nous-mêmes.
    Filtre sur les chemins commençant par `source`.

    Retourne (liste de {"path": Path, "album": str|None, ...métadonnées CSV
    éventuelles}, message d'avertissement ou None).

    Si master_scan.csv est introuvable ou illisible, repli automatique sur un
    scan direct du dossier (le fichier fonctionne quand même, avec un
    avertissement explicite plutôt qu'un échec silencieux).
    """
    csv_path = _tagcfg.master_csv_path
    if not csv_path.exists():
        return (
            [{"path": p} for p in _iter_audio_files(Path(source))],
            f"master_scan.csv introuvable ({csv_path}) — repli sur un scan direct du dossier.",
        )

    try:
        with open(csv_path, "r", newline="", encoding="utf-8", errors="replace") as f:
            sample = f.read(4096)
            f.seek(0)
            delim = _sniff_delimiter(sample)
            reader = csv.DictReader(f, delimiter=delim)
            if not reader.fieldnames:
                raise ValueError("CSV vide ou sans en-tête")

            lower_map = {name.lower(): name for name in reader.fieldnames}
            path_col = next((lower_map[c] for c in PATH_COL_CANDIDATES if c in lower_map), None)
            if path_col is None:
                raise ValueError(f"Aucune colonne de chemin reconnue parmi {reader.fieldnames}")
            album_col = next((lower_map[c] for c in ALBUM_COL_CANDIDATES if c in lower_map), None)
            # Colonnes déjà calculées par le scan principal — si présentes,
            # elles permettent d'éviter de rouvrir les fichiers déjà conformes
            # (voir optimisation dans _run()).
            cover_size_col = lower_map.get("cover_size")
            cover_format_col = lower_map.get("cover_format")
            cover_md5_col = lower_map.get("cover_md5")
            has_cover_col = lower_map.get("has_cover")

            source_norm = os.path.normpath(str(Path(source)))
            out = []
            for row in reader:
                p = row.get(path_col, "")
                if not p:
                    continue
                p_norm = os.path.normpath(str(Path(p)))
                # Respecte la frontière du chemin : "GoogleMusic" ne doit pas
                # matcher "GoogleMusicOLD" (bug de startswith() nu corrigé).
                if not (p_norm == source_norm or p_norm.startswith(source_norm + os.sep)):
                    continue
                if detect_filetype(Path(p)) is None:
                    continue
                entry = {"path": Path(p), "album": row.get(album_col) if album_col else None}
                if cover_size_col and row.get(cover_size_col):
                    entry["cover_size"] = row.get(cover_size_col)
                if cover_format_col and row.get(cover_format_col):
                    entry["cover_format"] = row.get(cover_format_col)
                if cover_md5_col and row.get(cover_md5_col):
                    entry["cover_md5"] = row.get(cover_md5_col)
                if has_cover_col:
                    entry["has_cover"] = row.get(has_cover_col)
                out.append(entry)
            return out, None
    except Exception as e:
        return (
            [{"path": p} for p in _iter_audio_files(Path(source))],
            f"Erreur de lecture de master_scan.csv ({csv_path}) : {e} — repli sur un scan direct du dossier.",
        )

CSV_FIELDS = [
    "path", "filetype", "album", "cover_format", "cover_width", "cover_height",
    "cover_size", "cover_md5", "needs_processing", "reason",
    "new_format", "new_width", "new_height", "new_size", "new_md5",
    "quality_used", "target_met", "written", "error",
]


# ---------------------------------------------------------------- état global (autonome)
# Volontairement un simple dict + verrou en mémoire — pas de dépendance au
# gestionnaire d'état de l'app principale. Suffisant pour un outil mono-
# utilisateur, mono-job (un seul job à la fois, comme le reste de ZimaCompare).

class JobState:
    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.dry_run = True
        self.source = ""
        self.processed = 0
        self.total = 0
        self.current_file = ""
        self.fps = 0.0
        self.eta_seconds = 0
        self.error = ""
        self.warning = ""
        self.started_at = 0.0
        self.ended_at = 0.0
        self._stop_flag = threading.Event()

    def as_dict(self) -> dict:
        return {
            "running": self.running, "dry_run": self.dry_run, "source": self.source,
            "processed": self.processed, "total": self.total, "current_file": self.current_file,
            "fps": self.fps, "eta_seconds": self.eta_seconds, "error": self.error,
            "warning": self.warning,
        }


STATE = JobState()


# ---------------------------------------------------------------- modèle

@dataclass
class Picture_:
    data: bytes
    mime: str
    fmt: str
    width: int
    height: int
    size: int
    kind: int = 3
    desc: str = ""
    encoding: int = 3
    _md5_cache: Optional[str] = field(default=None, repr=False, compare=False)

    def md5(self) -> str:
        if self._md5_cache is None:
            self._md5_cache = hashlib.md5(self.data).hexdigest()
        return self._md5_cache


def _picture_from_raw(data: bytes, mime: str, kind: int = 3, desc: str = "", encoding: int = 3) -> Picture_:
    img = Image.open(io.BytesIO(data))
    return Picture_(data=data, mime=mime, fmt=(img.format or "UNKNOWN"),
                     width=img.width, height=img.height, size=len(data),
                     kind=kind, desc=desc or "", encoding=encoding)


def detect_filetype(path: Path) -> Optional[str]:
    ext = path.suffix.lower()
    if ext == ".mp3":
        return "mp3"
    if ext == ".flac":
        return "flac"
    if ext == ".m4a":
        return "mp4"
    return None


def read_tags_and_pictures(path: Path, filetype: str) -> Tuple[str, List[Picture_]]:
    album = ""
    pics: List[Picture_] = []

    if filetype == "mp3":
        audio = MP3(path)
        if audio.tags is not None:
            frame = audio.tags.get("TALB")
            if frame and frame.text:
                album = str(frame.text[0])
            for apic in audio.tags.getall("APIC"):
                pics.append(_picture_from_raw(apic.data, apic.mime, apic.type, apic.desc, apic.encoding))

    elif filetype == "flac":
        audio = FLAC(path)
        vals = audio.get("album")
        album = vals[0] if vals else ""
        for pic in audio.pictures:
            pics.append(_picture_from_raw(pic.data, pic.mime, pic.type, pic.desc))

    elif filetype == "mp4":
        audio = MP4(path)
        if audio.tags is not None:
            vals = audio.tags.get("\xa9alb")
            album = vals[0] if vals else ""
            if "covr" in audio.tags:
                for cover in audio.tags["covr"]:
                    mime = "image/jpeg" if cover.imageformat == MP4Cover.FORMAT_JPEG else "image/png"
                    pics.append(_picture_from_raw(bytes(cover), mime))

    return album, pics


def write_pictures(path: Path, filetype: str, pics: List[Picture_]):
    """Remplace UNIQUEMENT les images de pochette embarquées (APIC / PICTURE /
    covr). Ne touche à aucun autre tag (titre, artiste, album, etc.) et ne
    supprime jamais le fichier audio lui-même — cette fonction n'appelle et
    n'appellera jamais os.remove()/Path.unlink() sur `path`. Seule la ou les
    pochette(s) sont modifiées, tout le reste du fichier est préservé tel quel."""
    assert path.exists(), "Le fichier audio doit exister : on ne fait que le modifier, jamais le recréer."

    if filetype == "mp3":
        audio = MP3(path)
        if audio.tags is None:
            audio.add_tags()
        audio.tags.delall("APIC")            # retire uniquement les anciennes pochettes
        for p in pics:
            audio.tags.add(APIC(encoding=p.encoding, mime=p.mime, type=p.kind,
                                 desc=p.desc, data=p.data))
        audio.save()                          # mutagen ne réécrit que les frames modifiées

    elif filetype == "flac":
        audio = FLAC(path)
        audio.clear_pictures()                # ne touche que le(s) bloc(s) PICTURE
        for p in pics:
            fp = FlacPicture()
            fp.data = p.data
            fp.type = p.kind
            fp.mime = p.mime
            fp.desc = p.desc
            fp.width = p.width
            fp.height = p.height
            audio.add_picture(fp)
        audio.save()

    elif filetype == "mp4":
        audio = MP4(path)
        if audio.tags is None:
            audio.add_tags()
        covers = []
        for p in pics:
            fmt = MP4Cover.FORMAT_JPEG if p.mime == "image/jpeg" else MP4Cover.FORMAT_PNG
            covers.append(MP4Cover(p.data, imageformat=fmt))
        audio.tags["covr"] = covers           # ne remplace que la clé "covr"
        audio.save()


# ---------------------------------------------------------------- dichotomie de qualité

def _find_best_quality(im: Image.Image, max_bytes: int, min_quality: int, max_quality: int) -> Tuple[bytes, int, bool]:
    cache: Dict[int, bytes] = {}

    def encode(q: int) -> bytes:
        if q not in cache:
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=q, optimize=True)
            cache[q] = buf.getvalue()
        return cache[q]

    data_at_max = encode(max_quality)
    if len(data_at_max) <= max_bytes:
        return data_at_max, max_quality, True

    data_at_min = encode(min_quality)
    if len(data_at_min) > max_bytes:
        return data_at_min, min_quality, False

    lo, hi = min_quality, max_quality
    best_q, best_data = min_quality, data_at_min
    while lo <= hi:
        mid = (lo + hi) // 2
        d = encode(mid)
        if len(d) <= max_bytes:
            best_q, best_data = mid, d
            lo = mid + 1
        else:
            hi = mid - 1
    return best_data, best_q, True


def needs_processing(pic: Picture_, max_bytes: int, force_all: bool, max_px: int = 0) -> Tuple[bool, str]:
    if force_all:
        return True, "reconversion forcée"
    if pic.fmt != "JPEG":
        return True, f"format {pic.fmt} (non JPEG)"
    if max_px and (pic.width > max_px or pic.height > max_px):
        return True, f"dimensions {pic.width}x{pic.height} > {max_px}px"
    if pic.size > max_bytes:
        return True, f"poids {pic.size} > limite {max_bytes}"
    return False, "déjà conforme"


def compress_picture(pic: Picture_, max_bytes: int, min_quality: int, max_quality: int,
                     max_px: int = 0, allow_downscale: bool = False,
                     downscale_ratio: float = 0.9, min_dimension: int = 300) -> Tuple[Picture_, int, bool]:
    img = Image.open(io.BytesIO(pic.data))
    img.load()
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        img = img.convert("RGBA")
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[-1])
        img = bg
    else:
        img = img.convert("RGB")
    # Redimensionnement a une cible pixel fixe (plus grand cote = max_px), sans agrandir.
    if max_px and max(img.width, img.height) > max_px:
        ratio = max_px / float(max(img.width, img.height))
        nw = max(1, int(round(img.width * ratio)))
        nh = max(1, int(round(img.height * ratio)))
        img = img.resize((nw, nh), Image.LANCZOS)
    data, q, target_met = _find_best_quality(img, max_bytes, min_quality, max_quality)
    # Repli : si encore trop lourd et autorise, reduire progressivement.
    if not target_met and allow_downscale:
        w, h = img.width, img.height
        while True:
            w = int(w * downscale_ratio); h = int(h * downscale_ratio)
            if w < min_dimension or h < min_dimension:
                break
            img = img.resize((w, h), Image.LANCZOS)
            data, q, target_met = _find_best_quality(img, max_bytes, min_quality, max_quality)
            if target_met:
                break
    new_pic = Picture_(data=data, mime="image/jpeg", fmt="JPEG", width=img.width, height=img.height,
                        size=len(data), kind=pic.kind, desc=pic.desc, encoding=pic.encoding)
    return new_pic, q, target_met


# ---------------------------------------------------------------- job de fond

def _iter_audio_files(root: Path):
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            if ext in (".mp3", ".flac", ".m4a"):
                yield Path(dirpath) / fn


def _run(root: Path, params: dict, apply_write: bool):
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Récupère la liste des fichiers depuis master_scan.csv (info déjà connue
    # de l'app principale) plutôt que de rescanner le disque nous-mêmes.
    candidates, warning = load_candidate_files(str(root))
    only = params.get("only_paths")
    if only:
        candidates = [c for c in candidates if os.path.normpath(str(c["path"])) in only]
    STATE.warning = warning or ""
    if warning:
        print(f"[covercompress] {warning}", file=sys.stderr)

    STATE.total = len(candidates)
    STATE.processed = 0
    rows = []
    t0 = time.time()

    for entry in candidates:
        if STATE._stop_flag.is_set():
            break
        p = entry["path"]
        STATE.processed += 1
        STATE.current_file = str(p)
        elapsed = time.time() - t0
        STATE.fps = round(STATE.processed / elapsed, 1) if elapsed > 0 else 0
        STATE.eta_seconds = int((STATE.total - STATE.processed) / STATE.fps) if STATE.fps > 0 else 0

        row = {k: "" for k in CSV_FIELDS}
        row["path"] = str(p)
        filetype = detect_filetype(p)
        row["filetype"] = filetype or ""
        try:
            if not p.exists():
                row["error"] = "fichier introuvable (référencé dans master_scan.csv mais absent du disque)"
                rows.append(row)
                continue

            # ---- Optimisation : si master_scan.csv connaît déjà le format/
            # poids/MD5 de la pochette, et qu'elle est déjà conforme, on
            # évite d'ouvrir et de reparser le fichier avec mutagen+Pillow.
            # Gain important sur une bibliothèque où la plupart des
            # pochettes sont déjà bonnes. Ne s'applique pas si force_all.
            shortcut_used = False
            if not params["force_all"] and entry.get("cover_format") and entry.get("cover_size"):
                try:
                    csv_size = int(float(entry["cover_size"]))
                    csv_fmt = str(entry["cover_format"]).upper()
                    has_cover = str(entry.get("has_cover", "True")).lower() not in ("false", "0", "")
                    csv_w = int(float(entry.get("cover_width") or 0))
                    csv_h = int(float(entry.get("cover_height") or 0))
                    _mpx = params["max_px"]
                    _dims_ok = (not _mpx) or (csv_w <= _mpx and csv_h <= _mpx and csv_w and csv_h)
                    if has_cover and csv_fmt == "JPEG" and csv_size <= params["max_bytes"] and _dims_ok:
                        row["album"] = entry.get("album") or ""
                        row["cover_format"] = csv_fmt
                        row["cover_size"] = csv_size
                        row["cover_md5"] = entry.get("cover_md5", "")
                        row["needs_processing"] = False
                        row["reason"] = "déjà conforme (info reprise de master_scan.csv, fichier non rouvert)"
                        shortcut_used = True
                except (ValueError, TypeError):
                    pass  # métadonnées CSV invalides/inattendues -> on retombe sur la lecture normale

            if shortcut_used:
                rows.append(row)
                continue

            # L'album connu de l'app principale (colonne du CSV) est réutilisé
            # tel quel s'il est disponible ; sinon on le relit nous-mêmes.
            known_album = entry.get("album")
            if known_album:
                album, pics = known_album, read_tags_and_pictures(p, filetype)[1]
            else:
                album, pics = read_tags_and_pictures(p, filetype)
            row["album"] = album
            if not pics:
                row["error"] = "aucune pochette"
                rows.append(row)
                continue
            pic = pics[0]
            row["cover_format"] = pic.fmt
            row["cover_width"] = pic.width
            row["cover_height"] = pic.height
            row["cover_size"] = pic.size
            row["cover_md5"] = pic.md5()

            needs, reason = needs_processing(pic, params["max_bytes"], params["force_all"], params["max_px"])
            row["needs_processing"] = needs
            row["reason"] = reason

            if needs:
                new_pic, q, target_met = compress_picture(
                    pic, params["max_bytes"], params["min_quality"], params["max_quality"],
                    max_px=params["max_px"], allow_downscale=params["allow_downscale"])
                row["new_format"] = new_pic.fmt
                row["new_width"] = new_pic.width
                row["new_height"] = new_pic.height
                row["new_size"] = new_pic.size
                row["new_md5"] = new_pic.md5()
                row["quality_used"] = q
                row["target_met"] = target_met

                if apply_write:
                    # Ne remplace QUE la pochette (pics[1:] = les éventuelles
                    # autres images embarquées, préservées telles quelles).
                    # Aucune suppression de fichier audio, jamais.
                    new_pics = [new_pic] + pics[1:]
                    if params.get("backup"):
                        bak = p.with_suffix(p.suffix + ".bak")
                        if not bak.exists():
                            bak.write_bytes(p.read_bytes())
                    write_pictures(p, filetype, new_pics)
                    row["written"] = True
        except Exception as e:
            row["error"] = str(e)
        rows.append(row)

    try:
        with open(RESULT_CSV, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=CSV_FIELDS, delimiter=";")
            w.writeheader()
            w.writerows(rows)
    except Exception:
        pass

    STATE.ended_at = time.time()
    STATE.running = False
    STATE.current_file = ""


def start_job(source: str, max_kb=800, min_quality=40, max_quality=95,
              force_all=False, backup=True, apply_write=False,
              max_px=0, allow_downscale=False, only_paths=None) -> str:
    with STATE.lock:
        if STATE.running:
            return "busy"
        root = Path(source)
        if not root.exists() or not root.is_dir():
            return "notfound"

        params = dict(max_bytes=int(max_kb) * 1024, min_quality=int(min_quality),
                      max_px=int(max_px), allow_downscale=bool(allow_downscale),
                      only_paths=set(only_paths) if only_paths else None,
                      max_quality=int(max_quality), force_all=bool(force_all), backup=bool(backup))
        if params["min_quality"] > params["max_quality"]:
            params["min_quality"], params["max_quality"] = params["max_quality"], params["min_quality"]

        STATE._stop_flag.clear()
        STATE.running = True
        STATE.dry_run = not apply_write
        STATE.source = source
        STATE.error = ""
        STATE.started_at = time.time()
        STATE.ended_at = 0.0
        STATE.processed = 0
        STATE.total = 0
        STATE.current_file = "Pré-scan…"

        t = threading.Thread(target=_run, args=(root, params, apply_write), daemon=True)
        t.start()
        return "started"


def stop_job() -> bool:
    if STATE.running:
        STATE._stop_flag.set()
        return True
    return False


def read_result_rows() -> List[dict]:
    if not RESULT_CSV.exists():
        return []
    with open(RESULT_CSV, "r", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f, delimiter=";"))


def result_info() -> dict:
    if not RESULT_CSV.exists():
        return {"exists": False, "rows": 0}
    rows = read_result_rows()
    needs = sum(1 for r in rows if r.get("needs_processing") == "True")
    written = sum(1 for r in rows if r.get("written") == "True")
    errors = sum(1 for r in rows if r.get("error"))
    dur = 0.0
    if STATE.ended_at and STATE.started_at and STATE.ended_at > STATE.started_at:
        dur = round(STATE.ended_at - STATE.started_at, 1)
    return {"exists": True, "rows": len(rows), "needs_processing": needs,
            "written": written, "errors": errors, "duration_seconds": dur}


def consistency_report() -> dict:
    rows = read_result_rows()
    by_album: Dict[str, Dict[str, List[str]]] = {}
    for r in rows:
        album = r.get("album", "")
        md5 = r.get("cover_md5", "")
        if not album or not md5:
            continue
        by_album.setdefault(album, {}).setdefault(md5, []).append(r.get("path", ""))
    divergent = {a: h for a, h in by_album.items() if len(h) > 1}
    return {
        "albums_checked": len(by_album),
        "albums_divergent": len(divergent),
        "details": [
            {"album": album, "hashes": [{"md5": h, "count": len(paths), "files": paths} for h, paths in groups.items()]}
            for album, groups in sorted(divergent.items())
        ],
    }
