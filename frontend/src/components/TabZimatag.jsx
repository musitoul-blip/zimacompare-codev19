import { useState, useEffect, useRef } from 'react'

const SRC = '/disks/HDD-Storage1/Media/GoogleMusic'
const FMTS = [['mp3', '🎵 MP3'], ['flac', '💿 FLAC'], ['m4a', '🍎 M4A']]

function fmtDur(s) { s = Math.round(s || 0); if (s < 60) return s + 's'; return Math.floor(s / 60) + 'm ' + (s % 60) + 's' }

export default function TabZimatag({ status }) {
  const [dirs, setDirs] = useState(null)
  const [idxLoading, setIdxLoading] = useState(false)
  const [filter, setFilter] = useState('')
  const [fmt, setFmt] = useState({ mp3: true, flac: true, m4a: true })
  const [limit, setLimit] = useState('')
  const [autoXls, setAutoXls] = useState(true)
  const [result, setResult] = useState(null)
  const [busy, setBusy] = useState(false)
  const [exporting, setExporting] = useState(false)
  const [msg, setMsg] = useState('')
  const [liveFmt, setLiveFmt] = useState(null)

  const isTagScan = status && status.app_state === 'SCANNING' && status.method === 'tagscan'
  const prevScan = useRef(false)
  const pollRef = useRef(null)
  const fmtPollRef = useRef(null)

  async function loadDirs(refresh) {
    setIdxLoading(true)
    try { const rs = await fetch('/api/tag/dirs' + (refresh ? '?refresh=1' : '')); setDirs(await rs.json()) }
    catch (e) { setMsg('Index dossiers : ' + e) } finally { setIdxLoading(false) }
  }
  async function loadResult() { try { const rs = await fetch('/api/tag/result'); setResult(await rs.json()) } catch (e) {} }
  useEffect(() => { loadDirs(false); loadResult() }, [])
  useEffect(() => {
    const was = prevScan.current; prevScan.current = isTagScan
    if (was && !isTagScan && status && status.app_state !== 'SCANNING') loadResult()
  }, [isTagScan])
  useEffect(() => () => { if (pollRef.current) clearInterval(pollRef.current) }, [])
  useEffect(() => {
    if (!isTagScan) { setLiveFmt(null); return }
    const tick = async () => { try { const p = await (await fetch('/api/tag/progress')).json(); setLiveFmt(p.fmt || null) } catch (e) {} }
    tick()
    fmtPollRef.current = setInterval(tick, 1200)
    return () => { if (fmtPollRef.current) clearInterval(fmtPollRef.current); fmtPollRef.current = null }
  }, [isTagScan])

  const selFmts = Object.keys(fmt).filter(k => fmt[k])
  const f = filter.trim().toLowerCase()
  let matched = []
  if (dirs && dirs.dirs) {
    for (const d of dirs.dirs) {
      if (f && !d.name.toLowerCase().includes(f)) continue
      const sel = selFmts.reduce((a, k) => a + d[k], 0)
      if (sel <= 0) continue
      matched.push({ name: d.name, mp3: d.mp3, flac: d.flac, m4a: d.m4a, sel })
    }
    matched.sort((a, b) => a.name.localeCompare(b.name))
  }
  const sum = { mp3: 0, flac: 0, m4a: 0, sel: 0 }
  for (const d of matched) { sum.mp3 += d.mp3; sum.flac += d.flac; sum.m4a += d.m4a; sum.sel += d.sel }
  const noMatch = !!(dirs && !idxLoading && selFmts.length && matched.length === 0)
  const canScan = selFmts.length > 0 && !noMatch && !busy && !isTagScan && !(limit.length > 0 && parseInt(limit, 10) <= 0)

  function finalizeAfterLaunch() {
    if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null }
    let n = 0
    pollRef.current = setInterval(async () => {
      n += 1; let st = 'IDLE'
      try { st = (await (await fetch('/api/status')).json()).app_state } catch (e) { return }
      if (st !== 'SCANNING' || n > 4000) {
        clearInterval(pollRef.current); pollRef.current = null
        await loadResult(); if (autoXls) await downloadExcel()
      }
    }, 1200)
  }
  async function startScan() {
    setMsg(''); setBusy(true)
    try {
      const body = { formats: selFmts, filter: filter.trim() || null, limit: limit ? parseInt(limit, 10) : null }
      const rs = await fetch('/api/tag/scan', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
      if (!rs.ok) { const e = await rs.json().catch(() => ({})); setMsg('Erreur : ' + (e.detail || rs.status)) } else { finalizeAfterLaunch() }
    } catch (e) { setMsg('Erreur reseau : ' + e) } finally { setBusy(false) }
  }
  async function abortScan() { try { await fetch('/api/tag/abort', { method: 'POST' }) } catch (e) {} }
  async function downloadExcel() {
    setExporting(true); setMsg('')
    try {
      const rs = await fetch('/api/tag/export.xlsx')
      if (!rs.ok) { const e = await rs.json().catch(() => ({})); setMsg('Export : ' + (e.detail || rs.status)); return }
      const blob = await rs.blob()
      const cd = rs.headers.get('Content-Disposition') || ''
      const mm = cd.match(/filename="?([^"]+)"?/)
      const name = mm ? mm[1] : 'ZimaTAG_Audit.xlsx'
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a'); a.href = url; a.download = name; document.body.appendChild(a); a.click(); a.remove(); URL.revokeObjectURL(url)
    } catch (e) { setMsg('Export reseau : ' + e) } finally { setExporting(false) }
  }

  const card = { border: '1px solid rgba(128,128,128,.28)', borderRadius: 12, padding: 18, marginBottom: 16 }
  const btn = { padding: '9px 18px', borderRadius: 8, border: 'none', cursor: 'pointer', fontWeight: 600, fontSize: 14 }
  const muted = { color: 'var(--muted, #8a8f98)' }
  const kpiVal = { fontSize: 30, fontWeight: 700, lineHeight: 1.1 }
  const kpiLbl = { fontSize: 12, ...muted, marginBottom: 2 }
  const chip = (on) => ({ display: 'inline-flex', alignItems: 'center', gap: 6, padding: '6px 12px', borderRadius: 20,
    border: '1px solid ' + (on ? '#2E86AB' : 'rgba(128,128,128,.35)'), background: on ? 'rgba(46,134,171,.15)' : 'transparent', cursor: 'pointer', userSelect: 'none', fontSize: 14 })
  const inp = { padding: '8px 10px', borderRadius: 8, border: '1px solid rgba(128,128,128,.35)', background: 'transparent', color: 'inherit', fontSize: 14 }
  const th = { textAlign: 'left', padding: '7px 10px', fontWeight: 600, fontSize: 12, color: 'var(--muted,#8a8f98)' }
  const thN = { ...th, textAlign: 'right' }
  const td = { padding: '6px 10px', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis', maxWidth: 360 }
  const tdN = { padding: '6px 10px', textAlign: 'right', fontVariantNumeric: 'tabular-nums' }

  function kpiBox(label, value, accent) {
    return (
      <div key={label} style={{ border: '1px solid ' + (accent || 'rgba(128,128,128,.3)'), borderRadius: 10, padding: '8px 14px', minWidth: 92 }}>
        <div style={{ fontSize: 11, color: 'var(--muted,#8a8f98)' }}>{label}</div>
        <div style={{ fontSize: 22, fontWeight: 700, color: accent || 'inherit' }}>{(value || 0).toLocaleString()}</div>
      </div>
    )
  }
  const r = result

  return (
    <div style={{ padding: 8, maxWidth: 820 }}>
      <h2 style={{ marginTop: 0 }}>🏷 ZimaTAG — audit des tags</h2>
      <p style={{ ...muted, marginTop: -6 }}>Analyse en lecture seule des tags audio et export d'un rapport Excel. Aucune écriture sur la source.</p>

      <div style={card}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <div style={{ fontWeight: 700, fontSize: 15 }}>Configuration du scan</div>
          <button style={{ ...btn, padding: '5px 10px', fontSize: 12, background: 'transparent', border: '1px solid rgba(128,128,128,.35)', color: 'inherit' }}
                  onClick={() => loadDirs(true)} disabled={idxLoading}>{idxLoading ? '⏳…' : '↻ Ré-indexer'}</button>
        </div>
        <div style={{ ...muted, fontSize: 13, margin: '6px 0 14px' }}>
          Source : <code>{SRC}</code>{dirs ? ' · ' + dirs.count + ' dossiers indexés' : (idxLoading ? ' · indexation…' : '')}
        </div>

        <div style={{ marginBottom: 14 }}>
          <div style={{ ...kpiLbl, marginBottom: 6 }}>Formats</div>
          <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap' }}>
            {FMTS.map(([k, lbl]) => (
              <span key={k} style={chip(fmt[k])} onClick={() => setFmt({ ...fmt, [k]: !fmt[k] })}>
                <input type="checkbox" checked={fmt[k]} readOnly style={{ pointerEvents: 'none' }} /> {lbl}
              </span>
            ))}
          </div>
        </div>

        <div style={{ marginBottom: 14 }}>
          <div style={{ ...kpiLbl, marginBottom: 6 }}>Filtre par nom de dossier (sous-chaîne, insensible à la casse)</div>
          <input value={filter} onChange={e => setFilter(e.target.value)} placeholder="ex. : indo, abba, live…" style={{ ...inp, width: '100%', maxWidth: 360 }} />
          {dirs && (
            <div style={{ marginTop: 12 }}>
              <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', marginBottom: 10 }}>
                {kpiBox('Dossiers', matched.length, noMatch ? '#DC3545' : '#2E86AB')}
                {kpiBox('Fichiers', sum.sel, noMatch ? '#DC3545' : '#28A745')}
                {fmt.mp3 && kpiBox('🎵 MP3', sum.mp3)}
                {fmt.flac && kpiBox('💿 FLAC', sum.flac)}
                {fmt.m4a && kpiBox('🍎 M4A', sum.m4a)}
              </div>
              {noMatch ? (
                <div style={{ color: '#DC3545', fontWeight: 600, fontSize: 13 }}>⚠ Aucun fichier ne correspond à ce filtre / ces formats — ajuste pour activer le scan.</div>
              ) : matched.length > 0 ? (
                <div style={{ border: '1px solid rgba(128,128,128,.25)', borderRadius: 10, overflow: 'hidden' }}>
                  <div style={{ maxHeight: 280, overflow: 'auto' }}>
                    <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
                      <thead>
                        <tr style={{ position: 'sticky', top: 0, background: 'var(--card, #16181d)' }}>
                          <th style={th}>Dossier</th>
                          {fmt.mp3 && <th style={thN}>MP3</th>}
                          {fmt.flac && <th style={thN}>FLAC</th>}
                          {fmt.m4a && <th style={thN}>M4A</th>}
                          <th style={thN}>Total</th>
                        </tr>
                      </thead>
                      <tbody>
                        {matched.slice(0, 200).map(d => (
                          <tr key={d.name} style={{ borderTop: '1px solid rgba(128,128,128,.15)' }}>
                            <td style={td} title={d.name}>{d.name}</td>
                            {fmt.mp3 && <td style={tdN}>{d.mp3 || ''}</td>}
                            {fmt.flac && <td style={tdN}>{d.flac || ''}</td>}
                            {fmt.m4a && <td style={tdN}>{d.m4a || ''}</td>}
                            <td style={{ ...tdN, fontWeight: 700 }}>{d.sel}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                  {matched.length > 200 && (
                    <div style={{ ...muted, fontSize: 12, padding: '6px 10px', borderTop: '1px solid rgba(128,128,128,.15)' }}>
                      … +{(matched.length - 200).toLocaleString()} autres dossiers (affine le filtre pour les afficher)
                    </div>
                  )}
                </div>
              ) : null}
            </div>
          )}
        </div>

        <div style={{ marginBottom: 16 }}>
          <div style={{ ...kpiLbl, marginBottom: 6 }}>Limite de fichiers (optionnel)</div>
          <input value={limit} onChange={e => setLimit(e.target.value.replace(/[^0-9]/g, ''))} placeholder="tous" style={{ ...inp, width: 120 }} />
        </div>

        <div style={{ display: 'flex', alignItems: 'center', gap: 14, flexWrap: 'wrap' }}>
          {!isTagScan ? (
            <button style={{ ...btn, background: '#2E86AB', color: '#fff', opacity: canScan ? 1 : .5, cursor: canScan ? 'pointer' : 'not-allowed' }}
                    onClick={startScan} disabled={!canScan}>{busy ? 'Démarrage…' : '🔍 Lancer le scan'}</button>
          ) : (
            <button style={{ ...btn, background: '#DC3545', color: '#fff' }} onClick={abortScan}>⏹ Arrêter</button>
          )}
          <label style={{ ...muted, fontSize: 13, display: 'inline-flex', alignItems: 'center', gap: 6, cursor: 'pointer' }}>
            <input type="checkbox" checked={autoXls} onChange={e => setAutoXls(e.target.checked)} /> Générer + télécharger l'Excel à la fin
          </label>
        </div>
        {!selFmts.length && <div style={{ color: '#DC3545', marginTop: 8, fontSize: 13 }}>Sélectionne au moins un format.</div>}
        {msg && <div style={{ color: '#DC3545', marginTop: 8 }}>{msg}</div>}
      </div>

      {isTagScan && (() => {
        const proc = status.processed || 0
        const tot = status.total || 0
        const pct = tot > 0 ? Math.min(100, Math.round(proc / tot * 100)) : 0
        return (
          <div style={card}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', marginBottom: 10 }}>
              <div style={{ fontWeight: 700, color: '#2E86AB' }}>⏳ Scan en cours…</div>
              <div style={{ fontSize: 13, ...muted }}>{tot > 0 ? pct + '%' : 'pré-scan…'}</div>
            </div>
            <div style={{ fontSize: 34, fontWeight: 800, lineHeight: 1.05, marginBottom: 6 }}>
              {proc.toLocaleString()} <span style={{ ...muted, fontSize: 20, fontWeight: 600 }}>/ {tot ? tot.toLocaleString() : '…'} fichiers</span>
            </div>
            <div style={{ background: 'rgba(128,128,128,.22)', borderRadius: 8, height: 14, overflow: 'hidden', marginBottom: 14 }}>
              <div style={{ width: pct + '%', height: '100%', background: '#2E86AB', transition: 'width .3s' }} />
            </div>
            <div style={{ display: 'flex', gap: 28, flexWrap: 'wrap' }}>
              <div><div style={kpiLbl}>Vitesse</div><div style={kpiVal}>{status.fps || 0} f/s</div></div>
              <div><div style={kpiLbl}>Reste estimé</div><div style={kpiVal}>{status.eta_seconds ? fmtDur(status.eta_seconds) : '—'}</div></div>
            </div>
            {liveFmt && (
              <div style={{ display: 'flex', gap: 24, flexWrap: 'wrap', marginTop: 14, fontSize: 14 }}>
                <span><span style={muted}>🎵 MP3</span> <b>{(liveFmt.done_mp3 || 0).toLocaleString()}</b> <span style={muted}>/ {(liveFmt.total_mp3 || 0).toLocaleString()}</span></span>
                <span><span style={muted}>💿 FLAC</span> <b>{(liveFmt.done_flac || 0).toLocaleString()}</b> <span style={muted}>/ {(liveFmt.total_flac || 0).toLocaleString()}</span></span>
                <span><span style={muted}>🍎 M4A</span> <b>{(liveFmt.done_m4a || 0).toLocaleString()}</b> <span style={muted}>/ {(liveFmt.total_m4a || 0).toLocaleString()}</span></span>
              </div>
            )}
            {status.current_file && (
              <div style={{ ...muted, fontSize: 12, marginTop: 12, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>📄 {status.current_file}</div>
            )}
          </div>
        )
      })()}

      {!isTagScan && r && r.exists && (
        <div style={card}>
          <div style={{ display: 'inline-flex', alignItems: 'center', gap: 8, padding: '6px 12px', borderRadius: 8, background: 'rgba(40,167,69,.15)', border: '1px solid #28A745', fontWeight: 700, marginBottom: 14 }}>✅ Dernier scan terminé</div>
          <div style={{ display: 'flex', gap: 36, flexWrap: 'wrap', marginBottom: 16 }}>
            <div><div style={kpiLbl}>Fichiers traités</div><div style={kpiVal}>{(r.rows || 0).toLocaleString()}</div></div>
            <div><div style={kpiLbl}>Durée totale</div><div style={kpiVal}>{r.duration_seconds ? fmtDur(r.duration_seconds) : '—'}</div></div>
            <div><div style={kpiLbl}>Vitesse moyenne</div><div style={kpiVal}>{r.fps_avg ? r.fps_avg + ' f/s' : '—'}</div></div>
          </div>
          {r.by_format && (
            <div style={{ marginBottom: 16 }}>
              <div style={{ ...kpiLbl, marginBottom: 6 }}>🎚 Répartition par format</div>
              <div style={{ display: 'flex', gap: 28, flexWrap: 'wrap' }}>
                <div><span style={muted}>🎵 MP3 </span><b>{(r.by_format.mp3 || 0).toLocaleString()}</b></div>
                <div><span style={muted}>💿 FLAC </span><b>{(r.by_format.flac || 0).toLocaleString()}</b></div>
                <div><span style={muted}>🍎 M4A </span><b>{(r.by_format.m4a || 0).toLocaleString()}</b></div>
                {r.by_format.autre ? <div><span style={muted}>autre </span><b>{r.by_format.autre}</b></div> : null}
              </div>
            </div>
          )}
          <button style={{ ...btn, background: '#28A745', color: '#fff' }} onClick={downloadExcel} disabled={exporting}>{exporting ? 'Génération…' : '📊 Télécharger l\'audit Excel'}</button>
        </div>
      )}
    </div>
  )
}
