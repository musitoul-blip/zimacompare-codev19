import base64
import csv
import io
import os
import shutil
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel
from PIL import Image

import compressor
from bluos_scanner import bluos_results

router = APIRouter(prefix="/api/cover")

COVER_ALLOW_WRITE = os.environ.get("COVER_ALLOW_WRITE", "false").strip().lower() == "true"
ALLOWED_BROWSE_PREFIXES = [
    p.strip() for p in os.environ.get(
        "ALLOWED_BROWSE_PREFIXES", "/disks,/network"
    ).split(",") if p.strip()
]

# = tagscan.TAG_SOURCE_DEFAULT, dupliqué volontairement pour ne pas coupler
# cover_routes.py à config.py (voir docstring compressor.py : modules greffés
# 100% indépendants) — périmètre d'une route destructive, doit rester explicite.
BAK_SOURCE_ROOT = Path("/disks/HDD-Storage1/Media/GoogleMusic")
BAK_DEST_ROOT = Path("/disks/HDD-Storage2/00_A_supp")


def _find_bak_files():
    """Liste (Path, size) de tous les *.bak sous BAK_SOURCE_ROOT. Seule source
    de vérité du périmètre, réutilisée par les 2 routes ci-dessous."""
    out = []
    if not BAK_SOURCE_ROOT.exists():
        return out
    for dirpath, _dirnames, filenames in os.walk(BAK_SOURCE_ROOT):
        for fn in filenames:
            if not fn.endswith(".bak"):
                continue
            fp = Path(dirpath) / fn
            try:
                out.append((fp, fp.stat().st_size))
            except OSError:
                continue
    out.sort(key=lambda t: str(t[0]))
    return out


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


def _scan_master_csv():
    """Lit master_scan.csv et regroupe les pistes par (albumartist, album) --
    la seule cle correcte a l'echelle de la bibliotheque (LOT 8 : `artist`
    est un tag PAR PISTE dans cette bibliotheque -- le regrouper fragmente
    un album en dizaines de faux groupes, 19689 vs 2158 mesures sur
    master_scan.csv ; `albumartist` est rempli sur 100% des lignes et donne
    le bon compte). Utilisee par /bluos/analysis (via _index_by_title),
    /api/cover/albums et /api/cover/album-paths.
    Retourne dict[(albumartist, album)] -> {albumartist, album, folder,
    cover_format, cover_size, cover_width, cover_height, paths[],
    has_cover_row}. Ne leve jamais ; dict vide si master_scan.csv
    introuvable/illisible."""
    csv_path = compressor._tagcfg.master_csv_path
    by_key = {}
    if not csv_path.exists():
        return by_key
    try:
        with open(str(csv_path), "r", encoding="utf-8", newline="", errors="replace") as f:
            sample = f.read(4096); f.seek(0)
            delim = compressor._sniff_delimiter(sample)
            rd = csv.DictReader(f, delimiter=delim)
            lm = {n.lower(): n for n in (rd.fieldnames or [])}
            c_albart = lm.get("albumartist")
            c_alb = lm.get("album"); c_fp = lm.get("filepath") or lm.get("path")
            c_has = lm.get("has_cover"); c_fmt = lm.get("cover_format")
            c_sz = lm.get("cover_size"); c_w = lm.get("cover_width"); c_h = lm.get("cover_height")
            for row in rd:
                alb = (row.get(c_alb) if c_alb else "") or ""
                if not alb:
                    continue
                albart = (row.get(c_albart) if c_albart else "") or ""
                fp = row.get(c_fp) if c_fp else ""
                entry = by_key.setdefault((albart, alb), {
                    "albumartist": albart, "album": alb,
                    "folder": "", "cover_format": "", "cover_size": 0,
                    "cover_width": 0, "cover_height": 0,
                    "paths": [], "has_cover_row": False,
                })
                if fp:
                    entry["paths"].append(fp)
                has = (row.get(c_has, "") if c_has else "").strip().lower() in ("yes", "true", "1")
                if has and not entry["has_cover_row"]:
                    try:
                        sz = int(float(row.get(c_sz) or 0))
                    except Exception:
                        sz = 0
                    entry["folder"] = os.path.dirname(fp) if fp else ""
                    entry["cover_format"] = (row.get(c_fmt) if c_fmt else "") or ""
                    entry["cover_size"] = sz
                    entry["cover_width"] = int(float(row.get(c_w) or 0)) if c_w else 0
                    entry["cover_height"] = int(float(row.get(c_h) or 0)) if c_h else 0
                    entry["has_cover_row"] = True
    except Exception:
        pass
    return by_key


def _index_by_title(by_key):
    """Projette by_key (cle correcte (albumartist, album)) vers un index par
    titre seul -- le seul vocabulaire que connait BluOS. Un titre porte par
    plusieurs albumartist distincts est un cas ambigu (LOT 8, mesure sur
    master_scan.csv : 23 titres sur 2131, dont 'Disco' Kylie Minogue/Pet
    Shop Boys et 'Greatest Hits' partage par 5 albumartist) : ne JAMAIS
    fusionner leurs paths (risque d'ecrire dans le mauvais album), le
    signaler explicitement plutot que deviner lequel BluOS visait.
    Retourne dict[album_title] -> {"entries": [...], "albumartists": [...]}"""
    idx = {}
    for (albart, alb), e in by_key.items():
        slot = idx.setdefault(alb, {"entries": [], "albumartists": []})
        slot["entries"].append(e)
        slot["albumartists"].append(albart)
    return idx


@router.get("/bluos/analysis")
def cover_bluos_analysis(max_kb: int = 1000):
    """Croise la liste BluOS (lecteur) avec master_scan.csv pour indiquer,
    par album fautif, si la pochette source est corrigeable par ZimaCover.
    Matching par titre seul contre BluOS (BluOS ne fournit que ca). Si un
    titre est porte par plusieurs albumartist differents dans le CSV
    (LOT 8, garde ambiguite) : ambiguous=true, paths=[] (jamais fusionnes),
    corrigeable=false -- correction desactivee ici, a faire depuis l'onglet
    Pochettes (LOT 8c) qui groupe par (albumartist, album) sans ambiguite."""
    max_bytes = int(max_kb) * 1024
    bluos = bluos_results()
    fautifs = [x for x in bluos.get("network", []) if x.get("status") and x.get("status") != "ok"]

    by_key = _scan_master_csv()
    by_title = _index_by_title(by_key)

    out = []
    for x in fautifs:
        title = x.get("title", "") or ""
        idx = by_title.get(title)
        corrigeable = False
        raison = "pochette introuvable sur le disque"
        folder = ""; cf = ""; cs = 0; cw = 0; ch = 0
        sample_path = ""; paths = []
        ambiguous = False

        if idx and len(idx["entries"]) > 1:
            ambiguous = True
            names = ", ".join(sorted(set(idx["albumartists"])))
            raison = (f"⚠ titre ambigu ({len(idx['entries'])} albums différents : {names}) "
                      "— correction désactivée, utiliser l'onglet Pochettes")
            info = idx["entries"][0]
            if info["has_cover_row"]:
                cf = info["cover_format"]; cs = info["cover_size"]
                cw = info["cover_width"]; ch = info["cover_height"]
            # folder / sample_path / paths restent vides : jamais de correction depuis cette route
        elif idx and idx["entries"][0]["has_cover_row"]:
            info = idx["entries"][0]
            folder = info["folder"]; cf = info["cover_format"]; cs = info["cover_size"]
            cw = info["cover_width"]; ch = info["cover_height"]
            paths = info["paths"]
            sample_path = paths[0] if paths else ""
            fmt_up = (cf or "").upper().replace("IMAGE/", "").strip()
            if fmt_up and fmt_up not in ("JPEG", "JPG"):
                corrigeable = True; raison = f"format {cf} (reconvertir en JPEG)"
            elif cs > max_bytes:
                corrigeable = True; raison = f"trop lourde ({round(cs/1024)} Ko)"
            elif cw and cw > 700:
                corrigeable = True; raison = f"grande ({cw}x{ch}, redimensionner)"
            else:
                corrigeable = False; raison = f"pochette {cw}x{ch} {round(cs/1024)} Ko (non corrigeable ici)"

        out.append({
            "artist": x.get("artist", ""), "title": title, "status": x.get("status", ""),
            "corrigeable": corrigeable, "raison": raison, "folder": folder,
            "cover_format": cf, "cover_size": cs, "cover_width": cw, "cover_height": ch,
            "sample_path": sample_path, "paths": paths, "ambiguous": ambiguous,
        })
    nb_corr = sum(1 for o in out if o["corrigeable"])
    return {"player": bluos.get("player"), "total": len(fautifs),
            "corrigeables": nb_corr, "albums": out}


@router.get("/albums")
def cover_albums():
    """Liste TOUTE la bibliotheque connue de master_scan.csv (pas seulement
    les albums fautifs BluOS), groupee par (albumartist, album) -- la cle
    correcte, sans ambiguite possible par construction. Ne renvoie PAS
    paths[] : 5.9 MB avec paths[] contre 804 KB sans, pour 2158 albums, et
    aucun gzip cote nginx pour absorber la difference. paths[] est
    recupere a la demande via /album-paths, uniquement pour l'album ouvert
    en apercu."""
    by_key = _scan_master_csv()
    out = []
    for (albart, alb), e in by_key.items():
        paths = e["paths"]
        out.append({
            "albumartist": albart, "album": alb, "folder": e["folder"],
            "cover_format": e["cover_format"], "cover_size": e["cover_size"],
            "cover_width": e["cover_width"], "cover_height": e["cover_height"],
            "sample_path": paths[0] if paths else "",
            "nb_tracks": len(paths),
        })
    return {"total": len(out), "albums": out}


@router.get("/album-paths")
def cover_album_paths(albumartist: str = "", album: str = ""):
    """Chemins absolus (paths[], pour only_paths) d'un album precis,
    identifie par (albumartist, album). Recupere a la demande par la
    modale d'apercu quand /albums ne les a pas fournis en bloc."""
    by_key = _scan_master_csv()
    entry = by_key.get((albumartist, album))
    if not entry:
        raise HTTPException(status_code=404, detail="album introuvable")
    return {"paths": entry["paths"]}


@router.get("/thumbnail")
def cover_thumbnail(folder: str = "", path: str = "", max_px: int = 700, max_kb: int = 1000, after: bool = True):
    """Pochette actuelle (avant) + version compressée simulée (après) en base64. Aucune écriture."""
    target = None
    if path:
        _check_allowed_prefix(path)
        target = Path(path)
    elif folder:
        _check_allowed_prefix(folder)
        try:
            for fp in sorted(Path(folder).iterdir()):
                if fp.suffix.lower() in (".mp3", ".flac", ".m4a"):
                    target = fp
                    break
        except Exception as e:
            raise HTTPException(status_code=404, detail=f"dossier illisible: {e}")
    if not target or not target.exists():
        raise HTTPException(status_code=404, detail="fichier introuvable")
    ft = compressor.detect_filetype(target)
    if not ft:
        raise HTTPException(status_code=415, detail="type non supporté")
    try:
        _, pics = compressor.read_tags_and_pictures(target, ft)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"lecture pochette: {e}")
    if not pics:
        raise HTTPException(status_code=404, detail="aucune pochette")
    pic = pics[0]

    def _thumb_b64(raw_bytes, box=220):
        im = Image.open(io.BytesIO(raw_bytes)); im.load()
        im = im.convert("RGB")
        im.thumbnail((box, box), Image.LANCZOS)
        buf = io.BytesIO(); im.save(buf, format="JPEG", quality=80)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")

    out = {"file": str(target), "before": {}, "after": {}}
    try:
        out["before"] = {"thumb": _thumb_b64(pic.data), "format": pic.fmt,
                         "width": pic.width, "height": pic.height, "size": pic.size}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"miniature avant: {e}")
    if after:
        try:
            newpic, q, met = compressor.compress_picture(pic, int(max_kb) * 1024, 40, 95,
                                                          max_px=int(max_px), allow_downscale=True)
            out["after"] = {"thumb": _thumb_b64(newpic.data), "format": newpic.fmt,
                            "width": newpic.width, "height": newpic.height,
                            "size": newpic.size, "quality": q, "target_met": met}
        except Exception as e:
            out["after"] = {"error": str(e)}
    return out


@router.get("/full")
def cover_full(folder: str = "", path: str = "", after: bool = False,
               max_px: int = 700, max_kb: int = 1000, allow_downscale: bool = True):
    """Sert la pochette en binaire (image/jpeg). after=false|true (avant/après compression simulée). Aucune écriture."""
    target = None
    if path:
        _check_allowed_prefix(path)
        target = Path(path)
    elif folder:
        _check_allowed_prefix(folder)
        try:
            for fp in sorted(Path(folder).iterdir()):
                if fp.suffix.lower() in (".mp3", ".flac", ".m4a"):
                    target = fp
                    break
        except Exception as e:
            raise HTTPException(status_code=404, detail=f"dossier illisible: {e}")
    if not target or not target.exists():
        raise HTTPException(status_code=404, detail="fichier introuvable")
    ft = compressor.detect_filetype(target)
    if not ft:
        raise HTTPException(status_code=415, detail="type non supporté")
    try:
        _, pics = compressor.read_tags_and_pictures(target, ft)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"lecture pochette: {e}")
    if not pics:
        raise HTTPException(status_code=404, detail="aucune pochette")
    pic = pics[0]
    if after:
        try:
            newpic, _q, _m = compressor.compress_picture(pic, int(max_kb) * 1024, 40, 95,
                                                         max_px=int(max_px), allow_downscale=allow_downscale)
            pic = newpic
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"compression: {e}")
    data = pic.data
    fmt = (pic.fmt or "").upper()
    if fmt not in ("JPEG", "JPG"):
        try:
            im = Image.open(io.BytesIO(pic.data)); im.load(); im = im.convert("RGB")
            buf = io.BytesIO(); im.save(buf, format="JPEG", quality=92)
            data = buf.getvalue()
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"conversion jpeg: {e}")
    return Response(content=data, media_type="image/jpeg")


@router.get("/preview-info")
def cover_preview_info(folder: str = "", path: str = "", max_px: int = 700,
                        max_kb: int = 1000, allow_downscale: bool = True):
    """Métadonnées de la pochette APRÈS compression simulée (format/dimensions/poids),
    en JSON, sans servir l'image. Aucune écriture."""
    target = None
    if path:
        _check_allowed_prefix(path)
        target = Path(path)
    elif folder:
        _check_allowed_prefix(folder)
        try:
            for fp in sorted(Path(folder).iterdir()):
                if fp.suffix.lower() in (".mp3", ".flac", ".m4a"):
                    target = fp
                    break
        except Exception as e:
            raise HTTPException(status_code=404, detail=f"dossier illisible: {e}")
    if not target or not target.exists():
        raise HTTPException(status_code=404, detail="fichier introuvable")
    ft = compressor.detect_filetype(target)
    if not ft:
        raise HTTPException(status_code=415, detail="type non supporté")
    try:
        _, pics = compressor.read_tags_and_pictures(target, ft)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"lecture pochette: {e}")
    if not pics:
        raise HTTPException(status_code=404, detail="aucune pochette")
    pic = pics[0]
    try:
        newpic, q, target_met = compressor.compress_picture(pic, int(max_kb) * 1024, 40, 95,
                                                             max_px=int(max_px), allow_downscale=allow_downscale)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"compression: {e}")
    return {
        "format": newpic.fmt, "width": newpic.width, "height": newpic.height,
        "size": newpic.size, "quality": q, "target_met": target_met,
    }


@router.get("/baks")
def cover_list_baks():
    """Liste les .bak sous BAK_SOURCE_ROOT (toute la bibliotheque), avec pour
    chacun already_archived (jumeau present dans BAK_DEST_ROOT) -- LOT 8e,
    pour piloter delete-redundant depuis le front. archive_count/archive_size
    donnent l'etat de l'archive elle-meme, pour purge-archive."""
    files = _find_bak_files()
    file_entries = []
    already_archived_count = 0
    for fp, size in files:
        rel = fp.relative_to(BAK_SOURCE_ROOT)
        dst = BAK_DEST_ROOT / rel
        archived = dst.exists()
        if archived:
            already_archived_count += 1
        file_entries.append({
            "path": str(fp), "album": fp.parent.name, "size": size,
            "already_archived": archived,
        })

    archive_sizes = []
    if BAK_DEST_ROOT.exists():
        for dirpath, _dirnames, filenames in os.walk(BAK_DEST_ROOT):
            for fn in filenames:
                if fn.endswith(".bak"):
                    try:
                        archive_sizes.append((Path(dirpath) / fn).stat().st_size)
                    except OSError:
                        continue

    return {
        "root": str(BAK_SOURCE_ROOT),
        "total": len(files),
        "total_size": sum(size for _fp, size in files),
        "already_archived_count": already_archived_count,
        "archive_count": len(archive_sizes),
        "archive_size": sum(archive_sizes),
        "files": file_entries,
    }


@router.post("/baks/delete-redundant")
def cover_delete_redundant_baks():
    """Supprime les .bak de BAK_SOURCE_ROOT qui ont deja un jumeau archive
    dans BAK_DEST_ROOT (meme chemin relatif). Ne supprime JAMAIS un .bak sans
    jumeau archive -- ce serait detruire la seule sauvegarde existante.
    L'archive elle-meme (BAK_DEST_ROOT) n'est jamais touchee par cette route."""
    if not COVER_ALLOW_WRITE:
        raise HTTPException(403, "Suppression des .bak désactivée (COVER_ALLOW_WRITE non activé)")
    if compressor.STATE.running:
        raise HTTPException(409, "Un job de correction est en cours")

    source_resolved = BAK_SOURCE_ROOT.resolve()
    deleted, skipped, errors = [], [], []
    for fp, _size in _find_bak_files():
        if not fp.name.endswith(".bak"):
            errors.append({"path": str(fp), "error": "extension inattendue"})
            continue
        rp = fp.resolve()
        if not str(rp).startswith(str(source_resolved) + os.sep):
            errors.append({"path": str(fp), "error": "hors périmètre BAK_SOURCE_ROOT"})
            continue
        rel = fp.relative_to(BAK_SOURCE_ROOT)
        dst = BAK_DEST_ROOT / rel
        if not dst.exists():
            skipped.append({"path": str(fp), "reason": "pas de jumeau archivé"})
            continue
        try:
            fp.unlink()
            deleted.append({"path": str(fp), "archived_twin": str(dst)})
        except Exception as e:
            errors.append({"path": str(fp), "error": str(e)})

    return {
        "deleted": len(deleted), "skipped": len(skipped), "errors": len(errors),
        "details_deleted": deleted, "details_skipped": skipped, "details_errors": errors,
    }


@router.post("/baks/purge-archive")
def cover_purge_archive():
    """Vide entierement l'archive BAK_DEST_ROOT (tous les *.bak) et nettoie les
    dossiers vides restants. IRREVERSIBLE : l'utilisateur a deja confirme
    (double confirmation cote front) que les corrections sont validees --
    cette route ne fait aucune verification de jumeau, contrairement a
    delete-redundant."""
    if not COVER_ALLOW_WRITE:
        raise HTTPException(403, "Purge de l'archive désactivée (COVER_ALLOW_WRITE non activé)")
    if compressor.STATE.running:
        raise HTTPException(409, "Un job de correction est en cours")

    dest_resolved = BAK_DEST_ROOT.resolve()
    deleted, errors = [], []
    if BAK_DEST_ROOT.exists():
        for dirpath, _dirnames, filenames in os.walk(BAK_DEST_ROOT):
            for fn in filenames:
                if not fn.endswith(".bak"):
                    continue
                fp = Path(dirpath) / fn
                rp = fp.resolve()
                if not str(rp).startswith(str(dest_resolved) + os.sep):
                    errors.append({"path": str(fp), "error": "hors périmètre BAK_DEST_ROOT"})
                    continue
                try:
                    fp.unlink()
                    deleted.append({"path": str(fp)})
                except Exception as e:
                    errors.append({"path": str(fp), "error": str(e)})

        for dirpath, _dirnames, _filenames in os.walk(BAK_DEST_ROOT, topdown=False):
            if dirpath == str(BAK_DEST_ROOT):
                continue
            try:
                if not os.listdir(dirpath):
                    os.rmdir(dirpath)
            except OSError:
                pass

    return {
        "deleted": len(deleted), "skipped": 0, "errors": len(errors),
        "details_deleted": deleted, "details_errors": errors,
    }


@router.post("/baks/move")
def cover_move_baks():
    """Deplace tous les .bak de BAK_SOURCE_ROOT vers BAK_DEST_ROOT, en preservant
    l'arborescence relative (shutil.move : les deux disques sont des filesystems
    distincts, os.rename echouerait en cross-device). Ne remplace JAMAIS un .bak
    deja present en destination (skip + signale, jamais d'ecrasement d'archive)."""
    if not COVER_ALLOW_WRITE:
        raise HTTPException(403, "Déplacement des .bak désactivé (COVER_ALLOW_WRITE non activé)")

    moved, skipped, errors = [], [], []
    for fp, _size in _find_bak_files():
        rel = fp.relative_to(BAK_SOURCE_ROOT)
        dst = BAK_DEST_ROOT / rel
        if dst.exists():
            skipped.append({"path": str(fp), "reason": f"déjà archivé ({dst})"})
            continue
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(fp), str(dst))
            moved.append({"from": str(fp), "to": str(dst)})
        except Exception as e:
            errors.append({"path": str(fp), "error": str(e)})

    return {
        "moved": len(moved), "skipped": len(skipped), "errors": len(errors),
        "details_moved": moved, "details_skipped": skipped, "details_errors": errors,
    }
