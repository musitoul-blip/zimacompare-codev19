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
from urllib.parse import quote
import pandas as pd
from datetime import datetime
from core import config

ROW_CAP = 800
SKIP_KEYS = {"music_tags"}
KPI_KEYS = {"kpi_dashboard", "kpi_years", "kpi_genres", "kpi_albumartists", "genre_stats"}
DANGER_KEYS = {
    "covers_invalid", "missing_metadata", "duplicates_md5",
    "duplicates_artist_title", "incomplete_albums", "duration_zero",
}
# Audits INFORMATIFS (listings/recaps, pas des defauts) : exclus du comptage
# des problemes, des badges d'onglet et des flags "Par dossier".
INFO_KEYS = {"cover_size", "quality_analysis"}
GROUP_LABELS = {
    "qualite": "Qualite", "integrite": "Integrite", "metadonnees": "Metadonnees",
    "doublons": "Doublons", "casse": "Casse", "images": "Pochettes", "donnees": "Donnees",
}


# ----------------------------------------------------------------------
# Donnees (reutilise ExcelExporter pour l'enrichissement)
# ----------------------------------------------------------------------
def _prepare():
    from export.excel_export import ExcelExporter
    from audit import AuditEngine
    csv_path = config.master_csv_path
    if not csv_path.exists():
        raise FileNotFoundError("master_scan.csv introuvable: %s" % csv_path)
    df = pd.read_csv(csv_path, sep=config.CSV_SEPARATOR,
                     encoding=config.CSV_ENCODING, low_memory=False)
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
        if "filepath" in data.columns:
            return set(os.path.dirname(str(x)) for x in data["filepath"])
        res = set()
        if "parent_folder" in data.columns:
            for v in data["parent_folder"].astype(str):
                res.update(pf2dir.get(v, []))
        elif "album" in data.columns:
            for v in data["album"].astype(str):
                res.update(alb2dir.get(v, []))
        return res

    flags = {}
    for group_name, sheets in groups.items():
        if group_name in ("cockpit", "kpi", "donnees"):
            continue
        for sheet_name, data_key in sheets:
            if data_key in KPI_KEYS or data_key in SKIP_KEYS or data_key in INFO_KEYS:
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


# ----------------------------------------------------------------------
# Rendu
# ----------------------------------------------------------------------
def export_to_html():
    from export.excel_export import ExcelExporter
    from audit import report_model
    exp = _prepare()
    ar = exp.audit_results
    df = exp.df_main
    groups = ExcelExporter.SHEET_GROUPS
    weights = ExcelExporter.HEALTH_WEIGHTS
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
    p.append("<style>" + _CSS + "</style></head><body>")
    p.append("<div id='zt' class='zt'>")

    # ---- header ----
    p.append("<header class='zhead'>")
    p.append("<div><h1>ZimaTAG &middot; rapport d'audit</h1>")
    p.append(f"<p class='zsub'>{total} fichiers &middot; {n_albums} albums &middot; genere le {now}</p></div>")
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
    p.append("<p class='zmut' style='font-size:12px'>Clique <b>copier</b> puis colle le chemin dans l'Explorateur / EZ CD. Le lien <i>ouvrir</i> ne fonctionne que si tu as <b>telecharge</b> ce rapport et l'ouvres en fichier local (mieux dans Firefox).</p>")
    p.append("<div id='dirList'>")
    for r in dirs:
        nb = len(r["labels"])
        badges = "".join(f"<span class='zb zb-{sev}'>{_html.escape(lbl)}</span>" for lbl, sev in r["labels"])
        winj = r["win"].replace("\\", "\\\\").replace("'", "\\'")
        p.append(
            f"<div class='zrow' data-name=\"{_html.escape(r['name'].lower())}\" data-audits='{nb}'>"
            f"<div class='zrow-m'><div class='zrow-t'>{_html.escape(r['name'])}</div>"
            f"<div class='zbadges'>{badges}</div>"
            f"<div class='zpathline'><span class='zpath'>{_html.escape(r['win'])}</span>"
            f"<button class='zbtn zmini' onclick=\"ztCopy(this,'{winj}')\">copier</button>"
            f"<a class='zlink' href=\"{_html.escape(r['uri'])}\">ouvrir</a>"
            f"<a class='zlink zezcd' href=\"{_html.escape(r['ezcd'])}\" title='ouvrir ce dossier dans Mp3tag'>Mp3tag</a></div></div>"
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
            p.append("<div class='zcard'>")
            p.append(f"<div class='zcard-h'><h3>{_html.escape(_clean_label(sheet_name))}{info_tag}</h3>"
                     f"<span class='zb {badge_cls}'>{n}</span></div>")
            if isinstance(data, pd.DataFrame) and not data.empty:
                p.append("<input class='zflt' placeholder='filtrer...' oninput='ztTblFilter(this)'>")
                p.append("<div class='ztw'>" + _table_html(data) + "</div>")
            else:
                p.append("<p class='zmut'>Rien a signaler.</p>")
            p.append("</div>")
        p.append("</div>")

    p.append("<footer class='zmut'>ZimaTAG &middot; rapport autoporte hors-ligne &middot; memes audits que l'export Excel</footer>")
    p.append("</div>")
    p.append("<script>" + _JS + "</script></body></html>")
    return "".join(p)


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
.zpanel{display:none;padding:18px 26px 40px;max-width:1180px}
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
.zpathline{display:flex;gap:8px;align-items:center;flex-wrap:wrap;min-width:0}
.zpath{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px;color:var(--zmut);background:var(--zbg);
padding:4px 8px;border-radius:6px;border:1px solid var(--zborder);overflow:hidden;text-overflow:ellipsis;
white-space:nowrap;max-width:100%}
.zbtn{background:transparent;border:1px solid var(--zborder);color:var(--zfg);border-radius:8px;padding:5px 10px;
font-size:12px;cursor:pointer}
.zbtn:hover{background:var(--zbg)}
.zmini{padding:3px 8px}
.zlink{font-size:12px;color:#2E86AB;text-decoration:none}
.zlink:hover{text-decoration:underline}
.zezcd{color:#D85A30;font-weight:600}
.ztw{overflow:auto;max-height:440px;border:1px solid var(--zborder);border-radius:8px}
table.ztbl{border-collapse:collapse;width:100%;font-size:12.5px}
table.ztbl thead th{position:sticky;top:0;background:var(--zbg);color:var(--zfg);text-align:left;padding:7px 9px;
border-bottom:1px solid var(--zborder);white-space:nowrap;cursor:pointer}
table.ztbl tbody td{padding:6px 9px;border-bottom:1px solid var(--zborder);max-width:360px;overflow:hidden;
text-overflow:ellipsis;white-space:nowrap}
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
(function(){var ds=document.getElementById('dirSearch'),po=document.getElementById('prioOnly');
function flt(){var q=(ds.value||'').toLowerCase(),prio=po.checked;
document.querySelectorAll('#dirList .zrow').forEach(function(r){
var okq=r.getAttribute('data-name').indexOf(q)>-1;
var okp=!prio||parseInt(r.getAttribute('data-audits'),10)>=2;
r.style.display=(okq&&okp)?'':'none';});}
if(ds)ds.addEventListener('input',flt);if(po)po.addEventListener('change',flt);})();
document.addEventListener('click',function(e){var th=e.target.closest&&e.target.closest('table.ztbl thead th');
if(!th)return;var tbl=th.closest('table');var idx=Array.prototype.indexOf.call(th.parentNode.children,th);
var body=tbl.tBodies[0];if(!body)return;var rows=Array.prototype.slice.call(body.rows);
var asc=th.getAttribute('data-asc')!=='1';
rows.sort(function(a,b){var x=a.cells[idx]?a.cells[idx].innerText:'';var y=b.cells[idx]?b.cells[idx].innerText:'';
var nx=parseFloat(x.replace(',','.')),ny=parseFloat(y.replace(',','.'));
if(!isNaN(nx)&&!isNaN(ny))return asc?nx-ny:ny-nx;return asc?x.localeCompare(y):y.localeCompare(x);});
rows.forEach(function(r){body.appendChild(r);});th.setAttribute('data-asc',asc?'1':'0');});
"""
