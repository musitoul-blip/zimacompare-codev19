"""
export/html_export.py - F18: rapport HTML v2 ZimaTAG (complement de l'Excel).

Autoporte 100% hors-ligne (CSS/JS/SVG inline, zero CDN). Consomme report_model
comme SOURCE UNIQUE (health score + top issues), reutilise ExcelExporter pour
l'enrichissement (zero duplication). Nouveautes v2 :
  - theme clair/sombre (bascule)
  - onglets (Vue d'ensemble en accueil) au lieu d'un long scroll
  - graphiques SVG inline (jauge, donut formats, barres problemes/categorie)
  - vue "Par dossier" : agregation des problemes par repertoire, chemin Windows
    copiable + lien file:// (bonus en local) + nb de fichiers + priorite
"""
import html as _html
import os
import re
import math
import base64 as _b64
from pathlib import Path as _Path
from urllib.parse import quote
import pandas as pd
from datetime import datetime
from core import audit_registry  # T10 Lot F2

ROW_CAP = 800
SKIP_KEYS = {"music_tags"}
KPI_KEYS = {"kpi_dashboard", "kpi_years", "kpi_genres", "kpi_albumartists", "genre_stats"}
try:
    from config import APP_VERSION
except Exception:
    APP_VERSION = "3.8.0"

DANGER_KEYS = {
    "covers_invalid", "missing_metadata", "duplicates_md5",
    "incomplete_albums", "duration_zero",
}
# Audits INFORMATIFS (listings/recaps, pas des defauts) : exclus du comptage
# des problemes, des badges d'onglet et des flags "Par dossier".
INFO_KEYS = {"album_name_consistency", "bitrate_anomalies", "case_inconsistency_album", "cover_size", "quality_analysis", "albumartist_vs_artist", "duplicates_artist_title", "bitrate_mixed_album", "id3_version_inconsistency", "albumartist_typo", "folder_artist_mismatch"}
PARDOSSIER_KEEP = {"bitrate_mixed_album", "id3_version_inconsistency", "albumartist_typo", "folder_artist_mismatch"}  # INFO pour score/badges, mais listes dans la carte + filtre Par dossier
GROUP_LABELS = {
    "qualite": "Qualite", "integrite": "Integrite", "metadonnees": "Metadonnees",
    "doublons": "Doublons", "casse": "Casse", "images": "Pochettes", "donnees": "Donnees",
    "informations": "Informations",  # T10 Lot C
}


# ----------------------------------------------------------------------
# Donnees (reutilise ExcelExporter pour l'enrichissement)
# ----------------------------------------------------------------------
def _prepare():
    """[LOT v20-5e] Charge depuis SQLite (master_scan.db) au lieu du CSV.
    SQLITE_COLUMNS reutilise depuis excel_export.py (import deja present
    sur cette meme ligne) -- evite une duplication de plus de la liste."""
    from export.excel_export import ExcelExporter, SQLITE_COLUMNS
    from audit import AuditEngine
    from core import db
    db_path = _Path(db.DB_PATH)
    if not db_path.exists():
        raise FileNotFoundError("master_scan.db introuvable: %s" % db_path)
    conn = db.connect()
    try:
        df = pd.read_sql(
            "SELECT " + ",".join(SQLITE_COLUMNS) + " FROM tracks ORDER BY id",
            conn,
        )
    finally:
        conn.close()
    exp = ExcelExporter()
    exp.df_main = df
    exp.audit_results = AuditEngine(df).run_all_audits()
    exp.audit_results["music_tags"] = df
    exp._enrich_audit_results()
    return exp


def _win(p):
    from export.excel_export import ExcelExporter
    try:
        return ExcelExporter._translate_path_for_windows(str(p))
    except Exception:
        return str(p or "").replace("/", "\\")


def _file_uri(winpath):
    # percent-encode pour ne pas casser sur # % & ? espaces / accents
    return "file:///" + quote(winpath.replace("\\", "/"), safe=":/")


def _ezcd_uri(winpath):
    # protocole perso ezcd: (chemin entierement encode -> decode par le wrapper)
    return "ezcd:" + quote(winpath, safe="")


def _dir_uri(winpath):
    # protocole perso zimadir: -> ouvre le dossier dans l explorateur Windows
    return "zimadir:" + quote(winpath, safe="")


def _clean_label(name):
    return re.sub(r"^[^0-9A-Za-zÀ-ÿ]+", "", str(name)).strip()


def _score_color(s):
    if s >= 85:
        return "#2E7D32"
    if s >= 60:
        return "#BA7517"
    return "#C62828"


# ----------------------------------------------------------------------
# Graphiques SVG inline (offline)
# ----------------------------------------------------------------------
def _svg_gauge(score):
    frac = max(0, min(100, score)) / 100.0
    dash = frac * 220.0
    col = _score_color(score)
    return (
        f"<svg width='150' height='92' viewBox='0 0 180 100' role='img' aria-label='score {score}'>"
        f"<path d='M20,90 A70,70 0 0 1 160,90' fill='none' stroke='var(--ztrack)' stroke-width='14' stroke-linecap='round'/>"
        f"<path d='M20,90 A70,70 0 0 1 160,90' fill='none' stroke='{col}' stroke-width='14' stroke-linecap='round' stroke-dasharray='{dash:.1f} 220'/>"
        f"<text x='90' y='82' text-anchor='middle' font-size='40' font-weight='500' fill='var(--zfg)'>{score}</text>"
        f"</svg>"
    )


def _svg_donut(parts):
    parts = [(l, v, c) for (l, v, c) in parts if v > 0]
    total = sum(v for _, v, _ in parts) or 1
    r = 60.0
    cx = cy = 80.0
    circ = 2 * math.pi * r
    out = [f"<svg width='150' height='150' viewBox='0 0 160 160' role='img' aria-label='repartition des formats'>"]
    off = 0.0
    for _, v, c in parts:
        seg = (v / total) * circ
        out.append(
            f"<circle cx='{cx}' cy='{cy}' r='{r}' fill='none' stroke='{c}' stroke-width='26' "
            f"stroke-dasharray='{seg:.2f} {circ - seg:.2f}' stroke-dashoffset='{-off:.2f}' "
            f"transform='rotate(-90 {cx} {cy})'/>"
        )
        off += seg
    out.append("</svg>")
    return "".join(out)


def _svg_hbars(items, color="#D85A30"):
    items = [(l, v) for (l, v) in items if v > 0]
    if not items:
        return "<p class='zmut'>Aucun probleme.</p>"
    mx = max(v for _, v in items) or 1
    w = 380.0
    lblw = 120.0
    barw = w - lblw - 46
    rowh = 30
    h = rowh * len(items) + 8
    out = [f"<svg width='100%' viewBox='0 0 {w:.0f} {h}' role='img' aria-label='problemes par categorie'>"]
    y = 6
    for label, v in items:
        bw = (v / mx) * barw
        out.append(f"<text x='0' y='{y + 15}' font-size='12' fill='var(--zmut)'>{_html.escape(str(label)[:16])}</text>")
        out.append(f"<rect x='{lblw:.0f}' y='{y + 4}' width='{bw:.1f}' height='15' rx='4' fill='{color}'/>")
        out.append(f"<text x='{lblw + bw + 6:.1f}' y='{y + 16}' font-size='12' fill='var(--zfg)'>{v}</text>")
        y += rowh
    out.append("</svg>")
    return "".join(out)


# ----------------------------------------------------------------------
# Donnees derivees
# ----------------------------------------------------------------------
def _formats_counts(df):
    if df is None or df.empty or "filepath" not in df.columns:
        return []
    ext = df["filepath"].astype(str).str.rsplit(".", n=1).str[-1].str.lower()
    vc = ext.value_counts()
    palette = {"flac": "#378ADD", "mp3": "#1D9E75", "m4a": "#EF9F27"}
    parts = []
    for fmt in ["flac", "mp3", "m4a"]:
        if fmt in vc:
            parts.append((fmt.upper(), int(vc[fmt]), palette[fmt]))
    autre = int(vc.drop(labels=[f for f in ["flac", "mp3", "m4a"] if f in vc], errors="ignore").sum())
    if autre > 0:
        parts.append(("Autre", autre, "#888780"))
    return parts


def _problems_by_category(ar, groups):
    from audit import report_model
    items = []
    for group_name, sheets in groups.items():
        if group_name in ("cockpit", "kpi", "donnees"):
            continue
        tot = 0
        for _, data_key in sheets:
            if data_key in KPI_KEYS or data_key in SKIP_KEYS or data_key in INFO_KEYS:
                continue
            tot += report_model.get_row_count(ar, data_key)
        if tot > 0:
            items.append((GROUP_LABELS.get(group_name, group_name), tot))
    items.sort(key=lambda x: x[1], reverse=True)
    return items


# ----------------------------------------------------------------------
# Agregation "Par dossier"
# ----------------------------------------------------------------------
def _dir_aggregate(ar, df, groups):
    if df is None or df.empty or "filepath" not in df.columns:
        return []
    fp = df["filepath"].astype(str)
    dirs = fp.map(os.path.dirname)
    pf = df["parent_folder"].astype(str) if "parent_folder" in df.columns else dirs
    alb = df["album"].astype(str) if "album" in df.columns else pf
    tmp = pd.DataFrame({"dir": dirs.values, "fp": fp.values, "parent": pf.values, "album": alb.values})
    dir_info = {}
    pf2dir = {}
    alb2dir = {}
    for d, g in tmp.groupby("dir"):
        parent = g["parent"].iloc[0]
        album = g["album"].iloc[0]
        dir_info[d] = {"n": len(g), "parent": parent, "album": album}
        pf2dir.setdefault(parent, []).append(d)
        alb2dir.setdefault(album, []).append(d)

    def resolve(data):
        cols = list(data.columns)
        if "filepath" in cols:
            return set(os.path.dirname(str(x)) for x in data["filepath"])
        res = set()
        for fc in ("parent_folder", "Dossier", "Chemin", "directory", "dossier", "chemin", "Repertoire", "Répertoire"):
            if fc in cols:
                for v in data[fc].astype(str):
                    v = v.strip()
                    if not v:
                        continue
                    if v in dir_info:
                        res.add(v)
                    elif v in pf2dir:
                        res.update(pf2dir[v])
                    elif "/" in v:
                        res.add(v)
                if res:
                    return res
        for ac in ("album", "Album"):
            if ac in cols:
                for v in data[ac].astype(str):
                    res.update(alb2dir.get(v, []))
                if res:
                    return res
        return res

    flags = {}
    for group_name, sheets in groups.items():
        if group_name in ("cockpit", "kpi", "donnees"):
            continue
        for sheet_name, data_key in sheets:
            if data_key in KPI_KEYS or data_key in SKIP_KEYS or (data_key in INFO_KEYS and data_key not in PARDOSSIER_KEEP):
                continue
            data = ar.get(data_key)
            if not isinstance(data, pd.DataFrame) or data.empty:
                continue
            label = _clean_label(sheet_name)
            sev = "danger" if data_key in DANGER_KEYS else "warn"
            for d in resolve(data):
                flags.setdefault(d, {})[label] = sev

    rows = []
    for d, labels in flags.items():
        i = dir_info.get(d, {})
        win = _win(d)
        rows.append({
            "name": i.get("parent") or i.get("album") or os.path.basename(d) or d,
            "win": win, "uri": _file_uri(win), "ezcd": _ezcd_uri(win),
            "labels": sorted(labels.items()), "n": i.get("n", 0),
        })
    rows.sort(key=lambda r: (-len(r["labels"]), -r["n"]))
    return rows


def _table_html(df):
    shown = df.head(ROW_CAP)
    try:
        t = shown.to_html(index=False, border=0, escape=True, classes="ztbl", na_rep="")
    except Exception as e:
        return "<p class='zmut'>table indisponible: %s</p>" % _html.escape(str(e))
    if len(df) > ROW_CAP:
        t += f"<p class='zmut'>{len(df)} lignes au total, {ROW_CAP} affichees.</p>"
    return t


LINK_DROP = {
    "bitrate_anomalies": set(),
    "albumartist_vs_artist": set(),
    "duplicates_artist_title": {"file_md5"},
    "cover_size": set(),                 # T10 Lot D (Q5 par fichier)
    "duration_zero": set(),              # T10 Lot D (Q5, vide sur lib actuelle)
    "invalid_year_format": set(),        # T10 Lot D (Q5, vide sur lib actuelle)
    "duplicates_md5": {"file_md5"},      # T10 Lot D (Q5, vide sur lib actuelle)
}


def _links_cell(folder):
    win = _win(folder)
    winj = win.replace("\\", "\\\\").replace("'", "\\'")
    return (
        f"<div class='zpathline'><span class='zpath'>{_html.escape(win)}</span>"
        f"<button class='zbtn zmini' onclick=\"ztCopy(this,'{winj}')\">copier</button>"
        f"<a class='zlink' href=\"{_html.escape(_file_uri(win))}\">ouvrir</a>"
        f"<a class='zlink zezcd' href=\"{_html.escape(_ezcd_uri(win))}\" title='ouvrir ce dossier dans Mp3tag'>Mp3tag</a>"
        f"<a class='zlink zexp' href=\"{_html.escape(_dir_uri(win))}\" title='ouvrir le dossier dans l explorateur'>📂 Explorer</a></div>"
    )


def _table_html_links(df, data_key):
    folder_col = "filepath"
    if folder_col not in df.columns:
        return _table_html(df)
    drop = LINK_DROP.get(data_key, set())
    shown = df.head(ROW_CAP)
    keep = [c for c in shown.columns if c != folder_col and c not in drop]
    out = ["<table class='ztbl'><thead><tr>"]
    for c in keep:
        out.append("<th>%s</th>" % _html.escape(str(c)))
    out.append("<th>Dossier</th></tr></thead><tbody>")
    for _, row in shown.iterrows():
        out.append("<tr>")
        for c in keep:
            v = row[c]
            cell = "" if pd.isna(v) else str(v)
            out.append("<td>%s</td>" % _html.escape(cell))
        folder = os.path.dirname(str(row.get(folder_col, "")))
        out.append("<td class='zdir'>%s</td>" % _links_cell(folder))
        out.append("</tr>")
    out.append("</tbody></table>")
    if len(df) > ROW_CAP:
        out.append("<p class='zmut'>%d lignes au total, %d affichees.</p>" % (len(df), ROW_CAP))
    return "".join(out)


# ----------------------------------------------------------------------
# Rendu
# ----------------------------------------------------------------------
def export_to_html():
    from export.excel_export import ExcelExporter
    from audit import report_model
    exp = _prepare()
    # T10 Lot F2 : classements lus depuis la base (edition UI live)
    global INFO_KEYS, KPI_KEYS, SKIP_KEYS, PARDOSSIER_KEEP  # T10 Lot G2
    try:
        audit_registry.init_and_seed()
        _ik = audit_registry.get_info_keys()
        _kk = audit_registry.get_kpi_keys()
        _sk = audit_registry.get_skip_keys()
        _pd = audit_registry.get_pardossier_keep()  # T10 Lot G2
        if _ik: INFO_KEYS = _ik
        if _kk: KPI_KEYS = _kk
        if _sk: SKIP_KEYS = _sk
        PARDOSSIER_KEEP = _pd  # T10 Lot G2 : peut etre vide (aucun INFO repeche)
    except Exception:
        pass
    ar = exp.audit_results
    df = exp.df_main
    groups = exp.SHEET_GROUPS
    weights = exp.HEALTH_WEIGHTS
    score, _pen = report_model.compute_health_score(ar, df, groups, weights)
    top = report_model.compute_top_issues(ar, groups)
    total = len(df)
    n_albums = 0
    try:
        n_albums = int(df["parent_folder"].nunique()) if "parent_folder" in df.columns else 0
    except Exception:
        pass
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    dirs = _dir_aggregate(ar, df, groups)
    n_dirs = len(dirs)
    n_problems = sum(report_model.get_row_count(ar, dk)
                     for g, sh in groups.items() if g not in ("cockpit", "kpi", "donnees")
                     for _, dk in sh if dk not in KPI_KEYS and dk not in SKIP_KEYS and dk not in INFO_KEYS)

    p = []
    p.append("<!doctype html><html lang='fr'><head><meta charset='utf-8'>")
    p.append("<meta name='viewport' content='width=device-width, initial-scale=1'>")
    p.append("<title>ZimaTAG - Rapport d'audit</title>")
    p.append(_favicon_link())
    p.append("<style>" + _CSS + "</style></head><body>")
    p.append("<div id='zt' class='zt'>")

    # ---- header ----
    p.append("<header class='zhead'>")
    p.append("<div class='zhead-l'>")
    p.append(_logo_img())
    p.append(f"<div><h1>ZimaCompare&Tag v{APP_VERSION} &middot; rapport d'audit</h1>")
    p.append(f"<p class='zsub'>{total} fichiers &middot; {n_albums} albums &middot; genere le {now}</p></div>")
    p.append("</div>")
    p.append("<div class='zhead-r'>")
    p.append("<button id='zth' class='zbtn' onclick='ztTheme()' aria-label='theme'>clair / sombre</button>")
    p.append("<div class='zgauge'>" + _svg_gauge(score) + "<span class='zmut'>health<br>score</span></div>")
    p.append("</div></header>")

    # ---- KPIs ----
    p.append("<div class='zkpis'>")
    for lbl, val in [("Fichiers", total), ("Albums", n_albums), ("Problemes", n_problems), ("Albums concernes", n_dirs)]:
        p.append(f"<div class='zkpi'><div class='zmut'>{lbl}</div><div class='zkpi-v'>{val}</div></div>")
    p.append("</div>")

    # ---- tabs ----
    tabs = [("ov", "Vue d'ensemble", None), ("dir", "Par dossier", n_dirs)]
    for g in groups:
        if g in ("cockpit", "kpi"):
            continue
        cnt = sum(report_model.get_row_count(ar, dk) for _, dk in groups[g]
                  if dk not in KPI_KEYS and dk not in SKIP_KEYS and dk not in INFO_KEYS)
        tabs.append((g, GROUP_LABELS.get(g, g), cnt if cnt else None))
    p.append("<div class='ztabs'>")
    for i, (tid, lbl, cnt) in enumerate(tabs):
        active = " active" if i == 0 else ""
        badge = f"<span class='zcnt'>{cnt}</span>" if cnt else ""
        p.append(f"<button class='ztab{active}' data-t='{tid}'>{_html.escape(lbl)} {badge}</button>")
    p.append("</div>")

    # ---- panel: overview ----
    p.append("<div class='zpanel active' data-p='ov'>")
    p.append("<div class='zgrid'>")
    p.append("<div class='zcard'><h3>Repartition des formats</h3>")
    fmts = _formats_counts(df)
    leg = " &middot; ".join(f"<span style='color:{c}'>&#9632;</span> {l} {v}" for l, v, c in fmts)
    p.append(f"<div class='zmut' style='margin:6px 0 10px'>{leg}</div>")
    p.append("<div style='text-align:center'>" + _svg_donut([(l, v, c) for l, v, c in fmts]) + "</div></div>")
    p.append("<div class='zcard'><h3>Problemes par categorie</h3>")
    p.append(_svg_hbars(_problems_by_category(ar, groups)) + "</div>")
    p.append("</div>")
    if top:
        p.append("<div class='zcard'><h3>Top problemes</h3><ol class='ztop'>")
        for label, count, _g in top:
            p.append(f"<li><span>{_html.escape(_clean_label(label))}</span><b>{count}</b></li>")
        p.append("</ol></div>")
    p.append("</div>")

    # ---- panel: par dossier ----
    p.append("<div class='zpanel' data-p='dir'>")
    p.append("<div class='zfilters'><input id='dirSearch' placeholder='filtrer un album ou un artiste...'>")
    p.append("<label class='zmut'><input type='checkbox' id='prioOnly'> priorite (&#8805;2 audits)</label></div>")
    _catc = {}
    for _r in dirs:
        for _lbl, _sev in _r["labels"]:
            _catc[_lbl] = _catc.get(_lbl, 0) + 1
    _cats_all = []
    _seen = set()
    for _g, _sheets in groups.items():
        if _g in ("cockpit", "kpi", "donnees"):
            continue
        for _sn, _dk in _sheets:
            if _dk in KPI_KEYS or _dk in SKIP_KEYS or (_dk in INFO_KEYS and _dk not in PARDOSSIER_KEEP):
                continue
            _lab = _clean_label(_sn)
            if _lab and _lab not in _seen:
                _seen.add(_lab)
                _cats_all.append((_lab, _g, _dk))
    _cats_all.sort(key=lambda _t: 0 if _catc.get(_t[0], 0) else 1)
    if _cats_all:
        p.append("<div class='zcats'>")
        p.append("<span class='zmut' style='font-size:12px;margin-right:4px'>Filtrer par categorie :</span>")
        for _lab, _gg, _dk2 in _cats_all:
            _cnt = _catc.get(_lab, 0)
            _v = _html.escape(_lab.strip().lower(), quote=True)
            _cls = "zcat" if _cnt else "zcat zcat0"
            _go = "<a class='zcatgo' href='#zcard-%s' data-t='%s' data-card='zcard-%s' title='voir le detail'>&#8599;</a>" % (_html.escape(_dk2, quote=True), _html.escape(_gg, quote=True), _html.escape(_dk2, quote=True))
            p.append("<span class='zcatwrap'><label class='%s'><input type='checkbox' class='zcatcb' value='%s'> %s <span class='zmut'>(%d)</span></label>%s</span>" % (_cls, _v, _html.escape(_lab), _cnt, _go))
        p.append("</div>")
    p.append("<p class='zmut' style='font-size:12px'>Clique <b>copier</b> puis colle le chemin dans l'Explorateur / EZ CD. Le lien <i>ouvrir</i> ne fonctionne que si tu as <b>telecharge</b> ce rapport et l'ouvres en fichier local (mieux dans Firefox).</p>")
    p.append("<div id='dirList'>")
    for r in dirs:
        nb = len(r["labels"])
        catstr = _html.escape("|" + "|".join(_l.strip().lower() for _l, _s in r["labels"]) + "|", quote=True)
        badges = "".join(f"<span class='zb zb-{sev}'>{_html.escape(lbl)}</span>" for lbl, sev in r["labels"])
        winj = r["win"].replace("\\", "\\\\").replace("'", "\\'")
        p.append(
            f"<div class='zrow' data-name=\"{_html.escape(r['name'].lower())}\" data-audits='{nb}' data-cats='{catstr}'>"
            f"<div class='zrow-m'><div class='zrow-t'>{_html.escape(r['name'])}</div>"
            f"<div class='zbadges'>{badges}</div>"
            f"<div class='zpathline'><span class='zpath'>{_html.escape(r['win'])}</span>"
            f"<button class='zbtn zmini' onclick=\"ztCopy(this,'{winj}')\">copier</button>"
            f"<a class='zlink' href=\"{_html.escape(r['uri'])}\">ouvrir</a>"
            f"<a class='zlink zezcd' href=\"{_html.escape(r['ezcd'])}\" title='ouvrir ce dossier dans Mp3tag'>Mp3tag</a>"
            f"<a class='zlink zexp' href=\"{_html.escape(_dir_uri(r['win']))}\" title='ouvrir le dossier dans l explorateur'>📂 Explorer</a></div></div>"
            f"<div class='zrow-n zmut'>{r['n']} fichiers</div></div>"
        )
    p.append("</div></div>")

    # ---- panels: par groupe ----
    for g in groups:
        if g in ("cockpit", "kpi"):
            continue
        p.append(f"<div class='zpanel' data-p='{g}'>")
        for sheet_name, data_key in groups[g]:
            if data_key in SKIP_KEYS:
                continue
            data = ar.get(data_key)
            n = report_model.get_row_count(ar, data_key)
            is_info = data_key in INFO_KEYS
            badge_cls = "zb-ok" if (is_info or not n) else "zb-danger"
            info_tag = " <span class='zmut' style='font-size:11px;font-weight:400'>info</span>" if is_info else ""
            p.append(f"<div class='zcard' id='zcard-{data_key}'>")
            p.append(f"<div class='zcard-h'><h3>{_html.escape(_clean_label(sheet_name))}{info_tag}</h3>"
                     f"<span class='zb {badge_cls}'>{n}</span></div>")
            if isinstance(data, pd.DataFrame) and not data.empty:
                p.append("<input class='zflt' placeholder='filtrer...' oninput='ztTblFilter(this)'>")
                _tbl = _table_html_links(data, data_key) if data_key in LINK_DROP else _table_html(data)
                p.append("<div class='ztw'>" + _tbl + "</div>")
            else:
                p.append("<p class='zmut'>Rien a signaler.</p>")
            p.append("</div>")
        p.append("</div>")

    p.append("<footer class='zmut'>ZimaTAG &middot; rapport autoporte hors-ligne &middot; memes audits que l'export Excel</footer>")
    p.append("</div>")
    p.append("<script>" + _JS + "</script></body></html>")
    return "".join(p)


_ICONE_DIR = _Path(os.environ.get("ZIMA_ICONE_DIR", "/app_data/icone"))


def _read_icon_b64(filename):
    """Lit icone/<filename> et renvoie son base64 (str), ou None si absent/illisible."""
    try:
        f = _ICONE_DIR / filename
        if f.is_file():
            return _b64.b64encode(f.read_bytes()).decode('ascii')
    except Exception:
        pass
    return None


def _favicon_link():
    """<link> favicon incruste en data-URI, ou '' si le fichier est absent."""
    b = _read_icon_b64("favicon.ico")
    if not b:
        return ""
    return ("<link rel='icon' type='image/x-icon' "
            "href='data:image/x-icon;base64,%s'>" % b)


def _logo_img(height=96):
    """<img> du logo d'en-tete (PNG redimensionne a `height` px, data-URI),
    ou '' si absent/illisible. Redimensionnement Pillow pour eviter d'incruster
    le PNG source (~1.4 Mo) tel quel."""
    try:
        f = _ICONE_DIR / "Icone zimacompare.png"
        if not f.is_file():
            return ""
        from io import BytesIO
        from PIL import Image
        try:
            _resample = Image.Resampling.LANCZOS
        except AttributeError:
            _resample = Image.LANCZOS
        im = Image.open(f)
        im.load()
        if im.mode not in ("RGB", "RGBA"):
            im = im.convert("RGBA")
        w, h = im.size
        if h > height:
            nw = max(1, int(round(w * height / float(h))))
            im = im.resize((nw, height), _resample)
        buf = BytesIO()
        im.save(buf, format="PNG", optimize=True)
        b = _b64.b64encode(buf.getvalue()).decode('ascii')
        return ("<img class='zlogo' alt='ZimaCompare&Tag' "
                "src='data:image/png;base64,%s'>" % b)
    except Exception:
        return ""


_CSS = r"""
*{box-sizing:border-box}
body{margin:0}
.zt{--zbg:#f4f5f9;--zsurf:#fff;--zfg:#1a1a2e;--zmut:#6b7080;--zborder:#e6e7ee;--ztrack:#e8e8ee;
font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:var(--zfg);background:var(--zbg);
line-height:1.5;min-height:100vh}
.zt.dark{--zbg:#14151a;--zsurf:#1e2027;--zfg:#e8e8ee;--zmut:#9aa0ad;--zborder:#2a2d36;--ztrack:#2a2d36}
.zt h1{font-size:20px;margin:0;font-weight:600}
.zt h3{font-size:15px;margin:0 0 8px;font-weight:600}
.zmut{color:var(--zmut)}
.zhead{display:flex;flex-wrap:wrap;gap:18px;align-items:center;justify-content:space-between;padding:22px 26px;
background:var(--zsurf);border-bottom:1px solid var(--zborder)}
.zsub{margin:5px 0 0;color:var(--zmut);font-size:13px}
.zhead-r{display:flex;align-items:center;gap:16px}
.zhead-l{display:flex;align-items:center;gap:14px}
.zlogo{height:48px;width:auto;display:block;border-radius:8px}
.zgauge{display:flex;align-items:center;gap:8px;font-size:12px}
.zkpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;padding:18px 26px 0}
.zkpi{background:var(--zsurf);border:1px solid var(--zborder);border-radius:12px;padding:12px 14px}
.zkpi-v{font-size:24px;font-weight:600;margin-top:2px}
.ztabs{display:flex;gap:2px;flex-wrap:wrap;border-bottom:1px solid var(--zborder);margin:18px 26px 0;
position:sticky;top:0;background:var(--zbg);z-index:5;padding-top:4px}
.ztab{background:transparent;border:none;border-bottom:2px solid transparent;padding:9px 13px;font-size:13px;
color:var(--zmut);cursor:pointer}
.ztab.active{color:var(--zfg);border-bottom-color:var(--zfg)}
.zcnt{font-size:11px;background:#fdecea;color:#C62828;border-radius:20px;padding:1px 7px;margin-left:2px}
.zt.dark .zcnt{background:#3a1f1f;color:#f09595}
.zpanel{display:none;padding:18px 26px 40px;max-width:none}
.zpanel.active{display:block}
.zgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:16px}
.zcard{background:var(--zsurf);border:1px solid var(--zborder);border-radius:14px;padding:14px 16px;margin-bottom:14px}
.zcard-h{display:flex;align-items:center;gap:10px}
.zcard-h h3{margin:0}
.ztop{margin:6px 0 0;padding-left:20px}
.ztop li{display:flex;justify-content:space-between;gap:10px;padding:3px 0;border-bottom:1px solid var(--zborder)}
.zfilters{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin-bottom:8px}
.zfilters input[type=text],#dirSearch,.zflt{width:100%;max-width:340px;padding:8px 10px;border:1px solid var(--zborder);
border-radius:8px;background:var(--zsurf);color:var(--zfg);font-size:13px;outline:none}
.zflt{max-width:none;margin-bottom:10px}
.zcats{display:flex;flex-wrap:wrap;gap:6px 12px;align-items:center;margin:0 0 12px}
.zcatwrap{display:inline-flex;align-items:center;gap:3px}
.zcat{display:inline-flex;align-items:center;gap:5px;font-size:12px;color:#d9534f;background:rgba(217,83,79,.12);border:1px solid #d9534f;border-radius:8px;padding:4px 9px;cursor:pointer}
.zcat input{margin:0}
.zcat0{color:#2e9e5b;background:rgba(46,158,91,.12);border-color:#2e9e5b;opacity:1}
.zcatgo{font-size:12px;line-height:1;text-decoration:none;color:var(--zfg);opacity:.6;border:1px solid var(--zborder);border-radius:6px;padding:3px 6px;cursor:pointer}
.zcatgo:hover{opacity:1;border-color:var(--zfg)}
.zrow{display:flex;gap:12px;align-items:flex-start;padding:12px;border:1px solid var(--zborder);border-radius:12px;
background:var(--zsurf);margin-bottom:10px}
.zrow-m{flex:1;min-width:0}
.zrow-t{font-weight:600}
.zrow-n{font-size:12px;white-space:nowrap;text-align:right}
.zbadges{display:flex;gap:6px;flex-wrap:wrap;margin:6px 0}
.zb{font-size:11px;padding:2px 8px;border-radius:8px;white-space:nowrap}
.zb-danger{background:#fdecea;color:#C62828}
.zb-warn{background:#fdf3e0;color:#8a5a0b}
.zb-ok{background:#e6f4ea;color:#2E7D32}
.zt.dark .zb-danger{background:#3a1f1f;color:#f09595}
.zt.dark .zb-warn{background:#3a2f1a;color:#EF9F27}
.zt.dark .zb-ok{background:#16301f;color:#9fe1cb}
.zpathline{display:flex;gap:8px;align-items:center;flex-wrap:nowrap;min-width:0;overflow:hidden}
.zpath{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px;color:var(--zmut);background:var(--zbg);
padding:4px 8px;border-radius:6px;border:1px solid var(--zborder);
white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0;flex:0 1 auto;max-width:100%}
.zbtn{background:transparent;border:1px solid var(--zborder);color:var(--zfg);border-radius:8px;padding:5px 10px;
font-size:12px;cursor:pointer}
.zbtn:hover{background:var(--zbg)}
.zmini{padding:3px 8px}
.zlink{font-size:12px;color:#2E86AB;text-decoration:none}
.zlink:hover{text-decoration:underline}
.zezcd{color:#D85A30;font-weight:600}
.zexp{color:#2E7D32;font-weight:600}
.zpathline>.zbtn,.zpathline>.zlink{flex:none}
table.ztbl td.zdir{max-width:none;white-space:nowrap}
.ztw{overflow:auto;max-height:440px;border:1px solid var(--zborder);border-radius:8px}
table.ztbl{border-collapse:collapse;width:100%;font-size:12.5px}
table.ztbl thead th{position:sticky;top:0;background:var(--zbg);color:var(--zfg);text-align:left;padding:7px 9px;
border-bottom:1px solid var(--zborder);white-space:nowrap;cursor:pointer}
table.ztbl tbody td{padding:6px 9px;border-bottom:1px solid var(--zborder);max-width:480px;
white-space:normal;overflow-wrap:anywhere;vertical-align:top}
footer{text-align:center;font-size:12px;padding:22px 12px}
"""

_JS = r"""
(function(){var m=window.matchMedia&&window.matchMedia('(prefers-color-scheme: dark)').matches;
document.getElementById('zt').classList.add(m?'dark':'light');})();
function ztTheme(){document.getElementById('zt').classList.toggle('dark');}
function ztCopy(btn,path){var ok=false;
try{if(navigator.clipboard&&window.isSecureContext){navigator.clipboard.writeText(path);ok=true;}}catch(e){}
if(!ok){try{var ta=document.createElement('textarea');ta.value=path;ta.setAttribute('readonly','');
ta.style.position='fixed';ta.style.top='-1000px';ta.style.opacity='0';document.body.appendChild(ta);
ta.focus();ta.select();ok=document.execCommand('copy');document.body.removeChild(ta);}catch(e){}}
var t=btn.textContent;btn.textContent=ok?'copie !':'selectionne+Ctrl+C';setTimeout(function(){btn.textContent=t;},1500);}
function ztTblFilter(inp){var q=(inp.value||'').toLowerCase();var tb=inp.parentNode.querySelector('table tbody');
if(!tb)return;Array.prototype.forEach.call(tb.rows,function(r){r.style.display=r.innerText.toLowerCase().indexOf(q)>-1?'':'none';});}
document.querySelectorAll('.ztab').forEach(function(b){b.addEventListener('click',function(){
document.querySelectorAll('.ztab').forEach(function(x){x.classList.remove('active');});
document.querySelectorAll('.zpanel').forEach(function(x){x.classList.remove('active');});
b.classList.add('active');var pn=document.querySelector(".zpanel[data-p='"+b.getAttribute('data-t')+"']");
if(pn)pn.classList.add('active');window.scrollTo(0,0);});});
document.querySelectorAll('.zcatgo').forEach(function(a){a.addEventListener('click',function(e){e.preventDefault();var t=a.getAttribute('data-t');document.querySelectorAll('.ztab').forEach(function(x){x.classList.remove('active');});document.querySelectorAll('.zpanel').forEach(function(x){x.classList.remove('active');});var tb=document.querySelector(".ztab[data-t='"+t+"']");if(tb)tb.classList.add('active');var pn=document.querySelector(".zpanel[data-p='"+t+"']");if(pn)pn.classList.add('active');var c=document.getElementById(a.getAttribute('data-card'));if(c)c.scrollIntoView({behavior:'smooth',block:'start'});});});
(function(){var ds=document.getElementById('dirSearch'),po=document.getElementById('prioOnly');
var cbs=Array.prototype.slice.call(document.querySelectorAll('.zcatcb'));
function flt(){var q=(ds.value||'').toLowerCase(),prio=po.checked;
var sel=cbs.filter(function(c){return c.checked;}).map(function(c){return c.value;});
document.querySelectorAll('#dirList .zrow').forEach(function(r){
var okq=r.getAttribute('data-name').indexOf(q)>-1;
var okp=!prio||parseInt(r.getAttribute('data-audits'),10)>=2;
var cats=r.getAttribute('data-cats')||'';
var okc=sel.length===0||sel.some(function(v){return cats.indexOf('|'+v+'|')>-1;});
r.style.display=(okq&&okp&&okc)?'':'none';});}
if(ds)ds.addEventListener('input',flt);if(po)po.addEventListener('change',flt);
cbs.forEach(function(c){c.addEventListener('change',flt);});})();
document.addEventListener('click',function(e){var th=e.target.closest&&e.target.closest('table.ztbl thead th');
if(!th)return;var tbl=th.closest('table');var idx=Array.prototype.indexOf.call(th.parentNode.children,th);
var body=tbl.tBodies[0];if(!body)return;var rows=Array.prototype.slice.call(body.rows);
var asc=th.getAttribute('data-asc')!=='1';
rows.sort(function(a,b){var x=a.cells[idx]?a.cells[idx].innerText:'';var y=b.cells[idx]?b.cells[idx].innerText:'';
var nx=parseFloat(x.replace(',','.')),ny=parseFloat(y.replace(',','.'));
if(!isNaN(nx)&&!isNaN(ny))return asc?nx-ny:ny-nx;return asc?x.localeCompare(y):y.localeCompare(x);});
rows.forEach(function(r){body.appendChild(r);});th.setAttribute('data-asc',asc?'1':'0');});
"""
