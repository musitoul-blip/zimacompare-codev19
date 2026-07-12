import { useState, useEffect, useRef } from 'react'
import { api, fmtSize } from '../api.js'

// --- Actions chemin (identiques au rapport d'audit ZimaTag) ---
function ztCopy(text, btn) {
  let ok = false
  try {
    if (navigator.clipboard && window.isSecureContext) { navigator.clipboard.writeText(text); ok = true }
  } catch (e) { /* fallback */ }
  if (!ok) {
    try {
      const ta = document.createElement('textarea')
      ta.value = text; ta.setAttribute('readonly', '')
      ta.style.position = 'fixed'; ta.style.top = '-1000px'; ta.style.opacity = '0'
      document.body.appendChild(ta); ta.focus(); ta.select()
      ok = document.execCommand('copy'); document.body.removeChild(ta)
    } catch (e) { /* ignore */ }
  }
  if (btn) {
    const t = btn.textContent
    btn.textContent = ok ? 'copie !' : 'Ctrl+C'
    setTimeout(() => { btn.textContent = t }, 1500)
  }
}
// file:/// (percent-encode, comme _file_uri) ; ezcd: / zimadir: (comme _ezcd_uri/_dir_uri)
const fileUri  = (win) => 'file:///' + encodeURI(win.replace(/\\/g, '/'))
const ezcdUri  = (win) => 'ezcd:' + encodeURIComponent(win)
const zimadirUri = (win) => 'zimadir:' + encodeURIComponent(win)

// ============================================================================
// Onglet BluOS Artwork Scanner (Lot 5)
// - IP editable + test connexion
// - panneau parametres editables (bluos_config, pilote par get_all_bluos_params)
// - bouton Scanner (progression + stop), tableau des albums fautifs + dossiers
// ============================================================================

const muted = { color: 'var(--muted)', fontSize: 13 }
const card = { border: '1px solid var(--border)', borderRadius: 8, padding: 16, marginBottom: 16 }

function ParamRow({ p, onSaved }) {
  const [val, setVal] = useState(p.value)
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState('')
  const dirty = String(val) !== String(p.value)
  async function save() {
    setBusy(true); setMsg('')
    try { await api.bluosParamSet(p.param_key, val); setMsg('✓'); onSaved(p.param_key, val) }
    catch (e) { setMsg('✗ ' + e.message) } finally { setBusy(false) }
  }
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '4px 0', flexWrap: 'wrap' }}>
      <span style={{ minWidth: 260, fontSize: 13 }}>{p.label}
        <span style={{ color: 'var(--muted)', fontSize: 11 }}> ({p.param_key})</span>
      </span>
      <input value={val} onChange={e => setVal(e.target.value)}
             style={{ width: 140, fontSize: 13 }} />
      <span style={{ color: 'var(--muted)', fontSize: 12, minWidth: 30 }}>{p.unit}</span>
      <button className="btn-primary" disabled={!dirty || busy} onClick={save}
              style={{ fontSize: 12, padding: '2px 8px' }}>{busy ? '...' : 'Enregistrer'}</button>
      {msg && <span style={{ fontSize: 12, color: msg[0] === '✓' ? 'var(--success)' : 'var(--danger)' }}>{msg}</span>}
    </div>
  )
}

function StatusBadge({ s }) {
  const map = {
    ok: ['var(--success)', 'OK'],
    missing: ['var(--danger)', 'Manquante'],
    placeholder: ['var(--warning)', 'Icone generique'],
    error: ['var(--danger)', 'Erreur'],
  }
  const [color, label] = map[s] || ['var(--muted)', s]
  return <span style={{ color, fontWeight: 600, fontSize: 12 }}>{label}</span>
}

export default function TabBluos({ status }) {
  const [params, setParams] = useState([])
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState('')
  const [scanning, setScanning] = useState(false)
  const [prog, setProg] = useState(null)
  const [results, setResults] = useState(null)
  const [msg, setMsg] = useState('')
  const [sourcePath, setSourcePath] = useState('')
  const [pathCheck, setPathCheck] = useState(null)  // {in_csv, matched_dirs} ou null
  const [pathChecking, setPathChecking] = useState(false)
  const [coverAnalysis, setCoverAnalysis] = useState(null)
  const [coverLoading, setCoverLoading] = useState(false)
  const [coverErr, setCoverErr] = useState('')
  const [settings, setSettings] = useState({ maxKb: 1000, maxPx: 700, allowDownscale: false })
  const [albumSettings, setAlbumSettings] = useState({})   // { [title]: {maxKb?,maxPx?,allowDownscale?} }
  const [previewFor, setPreviewFor] = useState(null)        // album en cours d'aperçu, ou null
  const [afterInfo, setAfterInfo] = useState(null)          // {format,width,height,size} ou null
  const [afterInfoLoading, setAfterInfoLoading] = useState(false)
  const [afterInfoErr, setAfterInfoErr] = useState('')
  const [applyBusy, setApplyBusy] = useState('')   // title en cours, ou ''
  const [applyMsg, setApplyMsg] = useState({})     // { [title]: message }
  const [jobProgress, setJobProgress] = useState(null)   // {running,processed,total,eta_seconds,fps} ou null
  const [jobResult, setJobResult] = useState(null)        // {written,errors,...} ou null
  const [baksOpen, setBaksOpen] = useState(false)
  const [baksLoading, setBaksLoading] = useState(false)
  const [baksErr, setBaksErr] = useState('')
  const [baksList, setBaksList] = useState(null)    // {root,total,total_size,files}
  const [baksMoving, setBaksMoving] = useState(false)
  const [baksReport, setBaksReport] = useState(null) // {moved,skipped,errors,...}
  const pollRef = useRef(null)
  const coverPollRef = useRef(null)   // polling du job de correction pochettes (LOT 5g)

  // param IP courant (extrait des params pour le champ dedie)
  const ipParam = params.find(p => p.param_key === 'bluos_ip')

  async function load() {
    setLoading(true); setErr('')
    try {
      const ps = await api.bluosParams()
      setParams(ps)
      // Pre-remplir le dossier local depuis le parametre bluos_source_path (Lot 8)
      const sp = ps.find(p => p.param_key === 'bluos_source_path')
      if (sp && sp.value && !sourcePath) setSourcePath(sp.value)
    }
    catch (e) { setErr(e.message) } finally { setLoading(false) }
  }
  useEffect(() => { load() }, [])

  // Verification live du dossier (present dans master_scan.csv ?) avec debounce (Lot 8)
  useEffect(() => {
    if (!sourcePath || !sourcePath.trim()) { setPathCheck(null); return }
    setPathChecking(true)
    const h = setTimeout(async () => {
      try { setPathCheck(await api.bluosCheckPath(sourcePath.trim())) }
      catch (e) { setPathCheck(null) }
      finally { setPathChecking(false) }
    }, 500)
    return () => clearTimeout(h)
  }, [sourcePath])

  // cleanup polling au demontage
  useEffect(() => () => {
    if (pollRef.current) clearInterval(pollRef.current)
    if (coverPollRef.current) clearInterval(coverPollRef.current)
  }, [])

  // Rafraichit les metadonnees "apres" de la modale d'apercu a chaque changement
  // de reglage (global ou par album), ou d'album affiche.
  useEffect(() => {
    if (!previewFor) { setAfterInfo(null); setAfterInfoErr(''); return }
    const s = effectiveSettings(previewFor.title)
    let alive = true
    setAfterInfoLoading(true); setAfterInfoErr('')
    api.coverPreviewInfo({ path: previewFor.sample_path, maxKb: s.maxKb, maxPx: s.maxPx, allowDownscale: s.allowDownscale })
      .then(info => { if (alive) setAfterInfo(info) })
      .catch(e => { if (alive) setAfterInfoErr(e.message) })
      .finally(() => { if (alive) setAfterInfoLoading(false) })
    return () => { alive = false }
  }, [previewFor, settings, albumSettings])

  function onParamSaved(k, v) {
    setParams(ps => ps.map(p => p.param_key === k ? { ...p, value: v } : p))
  }

  async function testConnection() {
    setMsg('Test de connexion...')
    try {
      // un scan sans source declenche la connexion ; on lit le player via results apres coup
      // ici on fait simple : on informe l'utilisateur d'utiliser Scanner
      setMsg('Utilisez « Scanner » : le nom du lecteur apparaitra dans les resultats.')
    } catch (e) { setMsg('✗ ' + e.message) }
  }

  function startPolling() {
    if (pollRef.current) clearInterval(pollRef.current)
    pollRef.current = setInterval(async () => {
      try {
        const st = await api.bluosStatus()
        setProg(st)
        if (!st.running) {
          clearInterval(pollRef.current); pollRef.current = null
          setScanning(false)
          try { setResults(await api.bluosResults()) } catch (e) { /* ignore */ }
          if (st.error) setMsg('✗ ' + st.error)
          else setMsg('Scan termine.')
        }
      } catch (e) { /* transitoire */ }
    }, 1500)
  }

  // Resync au (re)montage : si un scan tourne cote backend, reprendre le suivi ;
  // sinon, recharger d'eventuels resultats deja produits (retour d'onglet).
  useEffect(() => {
    let alive = true
    ;(async () => {
      try {
        const st = await api.bluosStatus()
        if (!alive) return
        if (st && st.running) {
          setScanning(true); setProg(st); startPolling()
        } else {
          try { const r = await api.bluosResults(); if (alive && r) setResults(r) } catch (e) { /* pas de resultats */ }
        }
      } catch (e) { /* backend indispo : on ignore */ }
    })()
    return () => { alive = false }
  }, [])
  async function startScan() {
    setMsg(''); setResults(null); setProg(null)
    try {
      const body = {}
      if (sourcePath.trim()) body.source_path = sourcePath.trim()
      await api.bluosScan(body)
      setScanning(true)
      startPolling()
    } catch (e) { setMsg('✗ ' + (e.message || 'erreur')) }
  }

  async function abortScan() {
    try { await api.bluosAbort(); setMsg('Interruption demandee...') }
    catch (e) { setMsg('✗ ' + e.message) }
  }

  async function analyzeCovers() {
    setCoverLoading(true); setCoverErr(''); setCoverAnalysis(null)
    try { setCoverAnalysis(await api.coverBluosAnalysis()) }
    catch (e) { setCoverErr(e.message) }
    finally { setCoverLoading(false) }
  }

  function effectiveSettings(title) {
    return { ...settings, ...(albumSettings[title] || {}) }
  }
  function setAlbumSetting(title, patch) {
    setAlbumSettings(m => ({ ...m, [title]: { ...(m[title] || {}), ...patch } }))
  }
  function resetAlbumSettings(title) {
    setAlbumSettings(m => { const c = { ...m }; delete c[title]; return c })
  }
  function openPreview(a) { setPreviewFor(a) }
  function closePreview() { setPreviewFor(null) }

  function fmtCoverEta(secs) {
    if (!secs || secs <= 0) return ''
    if (secs < 60) return `~${secs}s`
    const m = Math.floor(secs / 60)
    const s = secs % 60
    return `~${m}min ${s}s`
  }

  function startCoverPolling(title) {
    if (coverPollRef.current) clearInterval(coverPollRef.current)
    coverPollRef.current = setInterval(async () => {
      try {
        const pg = await api.coverProgress()
        setJobProgress(pg)
        if (pg.running) {
          const eta = pg.eta_seconds > 0 ? ` — ${fmtCoverEta(pg.eta_seconds)} restantes` : ''
          setApplyMsg(m => ({ ...m, [title]: `⏳ ${pg.processed}/${pg.total}${eta}` }))
        } else {
          clearInterval(coverPollRef.current); coverPollRef.current = null
          let result = null
          try { result = await api.coverResult() } catch (e) { /* ignore */ }
          setJobResult(result)
          const written = result?.written ?? 0
          const errors = result?.errors ?? 0
          setApplyMsg(m => ({
            ...m,
            [title]: errors > 0 ? `✗ ${errors} erreur(s)` : `✓ terminé — ${written} fichier(s)`,
          }))
          setApplyBusy('')
        }
      } catch (e) { /* transitoire, meme logique que startPolling (scan BluOS) */ }
    }, 1500)
  }

  async function applyCorrection(a) {
    const s = effectiveSettings(a.title)
    setApplyBusy(a.title)
    setApplyMsg(m => ({ ...m, [a.title]: '⏳ lancé' }))
    setJobProgress(null)
    setJobResult(null)
    try {
      await api.coverApply({
        source: a.folder,
        max_kb: s.maxKb,
        max_px: s.maxPx,
        allow_downscale: s.allowDownscale,
        only_paths: a.paths || [],
      })
      startCoverPolling(a.title)
    } catch (e) {
      const txt = e.status === 403
        ? '⏳ Écriture désactivée (COVER_ALLOW_WRITE=false)'
        : '✗ ' + e.message
      setApplyMsg(m => ({ ...m, [a.title]: txt }))
      setApplyBusy('')
    }
  }

  const flagged = results?.network ? results.network.filter(r => r.status !== 'ok') : []

  async function openBaks() {
    setBaksOpen(true); setBaksReport(null); setBaksErr(''); setBaksList(null); setBaksLoading(true)
    try { setBaksList(await api.coverBaks()) }
    catch (e) { setBaksErr(e.message) }
    finally { setBaksLoading(false) }
  }

  function closeBaks() {
    setBaksOpen(false); setBaksList(null); setBaksReport(null); setBaksErr('')
  }

  async function moveBaks() {
    setBaksMoving(true); setBaksErr('')
    try { setBaksReport(await api.coverBaksMove()) }
    catch (e) {
      setBaksErr(e.status === 403 ? '⏳ Écriture désactivée (COVER_ALLOW_WRITE=false)' : '✗ ' + e.message)
    } finally { setBaksMoving(false) }
  }

  return (
    <div>
      <h2 style={{ fontSize: 18, marginBottom: 4 }}>📻 BluOS Artwork Scanner</h2>
      <p style={muted}>Scanne un lecteur Bluesound (Node) pour reperer les pochettes manquantes ou generiques,
        puis diagnostique la bibliotheque locale (via master_scan.csv).</p>

      {/* --- Parametres editables --- */}
      <div style={card}>
        <h3 style={{ fontSize: 15, marginTop: 0 }}>Parametres</h3>
        {loading && <div style={muted}>Chargement...</div>}
        {err && <div style={{ color: 'var(--danger)' }}>Erreur : {err}</div>}
        {params.map(p => <ParamRow key={p.param_key} p={p} onSaved={onParamSaved} />)}
      </div>

      {/* --- Scan --- */}
      <div style={card}>
        <h3 style={{ fontSize: 15, marginTop: 0 }}>Scan</h3>
        <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap', marginBottom: 8 }}>
          <span style={muted}>Lecteur : <strong>{ipParam ? ipParam.value : '?'}</strong> (modifiable ci-dessus)</span>
        </div>
        <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap', marginBottom: 8 }}>
          <label style={muted}>Dossier local (optionnel, diagnostic fichiers) :</label>
          <input value={sourcePath} onChange={e => setSourcePath(e.target.value)}
                 placeholder="/disks/HDD-Storage1/Media/GoogleMusic"
                 style={{ width: 320, fontSize: 13 }} />
          {pathChecking && <span style={{ ...muted, fontSize: 12 }}>…</span>}
          {!pathChecking && pathCheck && pathCheck.in_csv &&
            <span style={{ color: 'var(--success)', fontSize: 13 }} title={`${pathCheck.matched_dirs} dossier(s) dans le scan`}>✓ {pathCheck.matched_dirs} dossiers</span>}
          {!pathChecking && pathCheck && !pathCheck.in_csv &&
            <span style={{ color: 'var(--danger)', fontSize: 13 }} title="Aucun dossier trouve dans master_scan.csv">✗ absent du scan</span>}
        </div>
        <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
          <button className="btn-primary" onClick={startScan} disabled={scanning}>
            {scanning ? 'Scan en cours...' : 'Scanner'}
          </button>
          {scanning && <button onClick={abortScan}>Arreter</button>}
          {msg && <span style={{ fontSize: 13, color: msg[0] === '✗' ? 'var(--danger)' : 'var(--muted)' }}>{msg}</span>}
        </div>
        {scanning && prog && (
          <div style={{ marginTop: 12 }}>
            <div style={{ background: 'var(--border)', borderRadius: 6, height: 10, overflow: 'hidden' }}>
              <div style={{ width: (prog.progress || 0) + '%', height: '100%', background: 'var(--accent, #4a9)' }} />
            </div>
            <div style={{ ...muted, fontSize: 12, marginTop: 4 }}>
              {prog.phase === 'network' ? 'Reseau' : prog.phase === 'library' ? 'Fichiers' : ''} — {prog.current_file || ''}
            </div>
          </div>
        )}
      </div>

      {/* --- Resultats reseau --- */}
      {results && (
        <div style={card}>
          <h3 style={{ fontSize: 15, marginTop: 0 }}>
            Albums fautifs {results.player && <span style={muted}>— {results.player.name} ({results.player.model})</span>}
          </h3>
          <div style={{ ...muted, marginBottom: 8 }}>{flagged.length} album(s) sur {results.network?.length || 0} analyse(s)</div>
          {flagged.length === 0 && <div style={{ color: 'var(--success)' }}>Aucun probleme detecte.</div>}
          {flagged.length > 0 && (
            <table style={{ width: '100%', fontSize: 13, borderCollapse: 'collapse' }}>
              <thead><tr style={{ textAlign: 'left', color: 'var(--muted)' }}>
                <th style={{ padding: 4 }}>Artiste</th><th style={{ padding: 4 }}>Album</th>
                <th style={{ padding: 4 }}>Statut</th><th style={{ padding: 4 }}>Detail</th>
              </tr></thead>
              <tbody>
                {flagged.map((r, i) => (
                  <tr key={i} style={{ borderTop: '1px solid var(--border)' }}>
                    <td style={{ padding: 4 }}>{r.artist}</td>
                    <td style={{ padding: 4 }}>{r.title}</td>
                    <td style={{ padding: 4 }}><StatusBadge s={r.status} /></td>
                    <td style={{ padding: 4, ...muted }}>
                      {r.thumb && <img src={r.thumb} alt="" style={{ height: 32, width: 32, verticalAlign: 'middle', marginRight: 6, borderRadius: 3 }} />}
                      {r.detail}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}

      {/* --- Corrections pochettes (LOT 4) --- */}
      {results && flagged.length > 0 && (
        <div style={card}>
          <h3 style={{ fontSize: 15, marginTop: 0 }}>Corrections pochettes</h3>
          <div style={{ display: 'flex', gap: 8, alignItems: 'center', marginBottom: 8 }}>
            <button className="btn-primary" onClick={analyzeCovers} disabled={coverLoading}>
              {coverLoading ? 'Analyse...' : '🖼 Analyser les corrections pochettes'}
            </button>
            <button onClick={openBaks} style={{ marginLeft: 'auto' }} disabled={!!applyBusy}>🗑 Gérer les .bak</button>
            {coverErr && <span style={{ color: 'var(--danger)', fontSize: 13 }}>✗ {coverErr}</span>}
          </div>
          {coverAnalysis && (
            <>
              <div style={{ ...muted, marginBottom: 8 }}>
                {coverAnalysis.corrigeables} / {coverAnalysis.total} corrigeables
              </div>
              <div style={{ display: 'flex', gap: 12, alignItems: 'center', marginBottom: 8, flexWrap: 'wrap' }}>
                <span style={muted}>Réglages par défaut :</span>
                <label style={{ fontSize: 13 }}>Poids max (Ko)
                  <input type="number" value={settings.maxKb}
                         onChange={e => setSettings(s => ({ ...s, maxKb: Number(e.target.value) }))}
                         style={{ width: 80, marginLeft: 4 }} />
                </label>
                <label style={{ fontSize: 13 }}>Dimension max (px)
                  <input type="number" value={settings.maxPx}
                         onChange={e => setSettings(s => ({ ...s, maxPx: Number(e.target.value) }))}
                         style={{ width: 80, marginLeft: 4 }} />
                </label>
                <label style={{ fontSize: 13 }}>
                  <input type="checkbox" checked={settings.allowDownscale}
                         onChange={e => setSettings(s => ({ ...s, allowDownscale: e.target.checked }))} />
                  {' '}Réduire davantage si le poids max n'est pas atteint
                </label>
                <span style={{ fontSize: 11, color: 'var(--muted)' }}>
                  (la dimension max s'applique toujours — borne dure. Cette case autorise une réduction
                  supplémentaire des dimensions, par paliers de 15%, jusqu'à 300px minimum, si le poids
                  reste au-dessus du maximum après compression JPEG)
                </span>
              </div>
              <table style={{ width: '100%', fontSize: 13, borderCollapse: 'collapse' }}>
                <thead><tr style={{ textAlign: 'left', color: 'var(--muted)' }}>
                  <th style={{ padding: 4 }}>Artiste</th><th style={{ padding: 4 }}>Album</th>
                  <th style={{ padding: 4 }}>Corrigeable</th><th style={{ padding: 4 }}>Raison</th>
                  <th style={{ padding: 4 }}>Dimensions</th><th style={{ padding: 4 }}></th>
                </tr></thead>
                <tbody>
                  {coverAnalysis.albums.map((a, i) => (
                    <tr key={i} style={{ borderTop: '1px solid var(--border)' }}>
                      <td style={{ padding: 4 }}>{a.artist}</td>
                      <td style={{ padding: 4 }}>{a.title}</td>
                      <td style={{ padding: 4 }}>
                        <span style={{ color: a.corrigeable ? 'var(--success)' : 'var(--muted)', fontWeight: 600 }}>
                          {a.corrigeable ? 'Oui' : 'Non'}
                        </span>
                      </td>
                      <td style={{ padding: 4, ...muted }}>{a.raison}</td>
                      <td style={{ padding: 4, ...muted }}>
                        {a.cover_width && a.cover_height ? `${a.cover_width}×${a.cover_height}` : ''}
                        {a.cover_size ? ` · ${Math.round(a.cover_size / 1024)} Ko` : ''}
                      </td>
                      <td style={{ padding: 4 }}>
                        {a.corrigeable && a.paths && a.paths.length > 0 && (
                          <button onClick={() => openPreview(a)} style={{ fontSize: 12, padding: '2px 8px' }}>
                            👁 Aperçu
                          </button>
                        )}
                        {applyMsg[a.title] && <span style={{ fontSize: 12, marginLeft: 6 }}>{applyMsg[a.title]}</span>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          )}
        </div>
      )}

      {/* --- Diagnostic dossiers (volet B) enrichi (Lot 7) --- */}
      {results && results.folders && results.folders.length > 0 && (
        <div style={card}>
          <h3 style={{ fontSize: 15, marginTop: 0 }}>Diagnostic des dossiers ({results.folders.length})</h3>
          {results.folders.map((f, i) => {
            const win = f.win_path || f.folder
            const dims = (f.cover_width && f.cover_height) ? `${f.cover_width}×${f.cover_height}px` : ''
            const kb = f.cover_size ? `${Math.round(f.cover_size / 1024)} Ko` : ''
            const meta = [f.cover_format, dims, kb,
              f.cover_count ? `${f.cover_count} pochette${f.cover_count > 1 ? 's' : ''}` : '',
              (f.distinct_covers > 1) ? `${f.distinct_covers} images distinctes` : ''
            ].filter(Boolean).join(' · ')
            return (
              <div key={i} style={{ borderTop: i ? '1px solid var(--border)' : 'none', padding: '8px 0' }}>
                <div style={{ fontSize: 13, fontWeight: 600 }}>{f.folder.split('/').pop()}
                  {f.matched_network_album && <span style={muted}> — album fautif sur le lecteur</span>}
                </div>
                {meta && <div style={{ ...muted, fontSize: 12 }}>{meta}</div>}
                {f.issues && f.issues.map((it, j) => <div key={'i' + j} style={{ fontSize: 12, color: 'var(--danger)' }}>• {it}</div>)}
                {f.notes && f.notes.map((nt, j) => <div key={'n' + j} style={{ fontSize: 12, color: 'var(--warning)' }}>◦ {nt}</div>)}
                <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginTop: 4, flexWrap: 'wrap' }}>
                  <code style={{ fontSize: 12, background: 'var(--border)', padding: '1px 6px', borderRadius: 3 }}>{win}</code>
                  <button onClick={e => ztCopy(win, e.currentTarget)} style={{ fontSize: 11, padding: '1px 6px' }}>copier</button>
                  <a href={fileUri(win)} style={{ fontSize: 11 }}>ouvrir</a>
                  <a href={ezcdUri(win)} style={{ fontSize: 11 }} title="ouvrir ce dossier dans Mp3tag">Mp3tag</a>
                  <a href={zimadirUri(win)} style={{ fontSize: 11 }} title="ouvrir dans l'explorateur">📂 Explorer</a>
                </div>
              </div>
            )
          })}
        </div>
      )}

      {baksOpen && (
        <div style={{
          position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.5)',
          display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 1000,
        }} onClick={closeBaks}>
          <div style={{
            background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 8,
            padding: 20, width: 560, maxWidth: '90vw', maxHeight: '80vh',
            display: 'flex', flexDirection: 'column',
          }} onClick={e => e.stopPropagation()}>
            <h3 style={{ marginTop: 0, fontSize: 15 }}>Fichiers .bak</h3>
            {baksLoading && <div style={muted}>Chargement...</div>}
            {baksErr && <div style={{ color: 'var(--danger)', fontSize: 13, marginBottom: 8 }}>✗ {baksErr}</div>}

            {!baksReport && baksList && (
              <>
                <div style={{ ...muted, marginBottom: 8 }}>
                  {baksList.total} fichier(s), {fmtSize(baksList.total_size)} — {baksList.root}
                </div>
                <div style={{ overflowY: 'auto', flex: 1, border: '1px solid var(--border)', borderRadius: 4 }}>
                  {baksList.files.length === 0 && <div style={{ ...muted, padding: 8 }}>Aucun .bak.</div>}
                  {baksList.files.map((f, i) => (
                    <div key={i} style={{ display: 'flex', justifyContent: 'space-between', gap: 8, padding: '4px 8px', borderTop: i ? '1px solid var(--border)' : 'none', fontSize: 12 }}>
                      <span>{f.album} / {f.path.split('/').pop()}</span>
                      <span style={muted}>{fmtSize(f.size)}</span>
                    </div>
                  ))}
                </div>
              </>
            )}

            {baksReport && (
              <div style={{ fontSize: 13 }}>
                <div style={{ color: 'var(--success)' }}>{baksReport.moved} déplacé(s)</div>
                {baksReport.skipped > 0 && <div style={{ color: 'var(--warning)' }}>{baksReport.skipped} ignoré(s) (déjà archivés)</div>}
                {baksReport.errors > 0 && <div style={{ color: 'var(--danger)' }}>{baksReport.errors} erreur(s)</div>}
              </div>
            )}

            <div style={{ display: 'flex', gap: 8, marginTop: 12, justifyContent: 'flex-end' }}>
              {!baksReport && (
                <button className="btn-primary" onClick={moveBaks}
                        disabled={baksMoving || baksLoading || !baksList || baksList.total === 0}>
                  {baksMoving ? 'Déplacement...' : 'Déplacer vers 00_A_supp'}
                </button>
              )}
              <button onClick={closeBaks}>{baksReport ? 'Fermer' : 'Annuler'}</button>
            </div>
          </div>
        </div>
      )}

      {previewFor && (() => {
        const s = effectiveSettings(previewFor.title)
        const common = { path: previewFor.sample_path, maxKb: s.maxKb, maxPx: s.maxPx, allowDownscale: s.allowDownscale }
        return (
          <div style={{
            position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.5)',
            display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 1000,
          }} onClick={closePreview}>
            <div style={{
              background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 8,
              padding: 20, width: '95vw', height: '90vh', overflow: 'hidden',
              display: 'flex', flexDirection: 'column',
            }} onClick={e => e.stopPropagation()}>
              <h3 style={{ marginTop: 0, marginBottom: 8, fontSize: 15, flex: '0 0 auto' }}>
                {previewFor.artist} — {previewFor.title}
              </h3>

              <div style={{ display: 'flex', gap: 12, alignItems: 'center', marginBottom: 12, flexWrap: 'wrap', flex: '0 0 auto' }}>
                <label style={{ fontSize: 13 }}>Poids max (Ko)
                  <input type="number" value={s.maxKb}
                         onChange={e => setAlbumSetting(previewFor.title, { maxKb: Number(e.target.value) })}
                         style={{ width: 80, marginLeft: 4 }} />
                </label>
                <label style={{ fontSize: 13 }}>Dimension max (px)
                  <input type="number" value={s.maxPx}
                         onChange={e => setAlbumSetting(previewFor.title, { maxPx: Number(e.target.value) })}
                         style={{ width: 80, marginLeft: 4 }} />
                </label>
                <label style={{ fontSize: 13 }}>
                  <input type="checkbox" checked={s.allowDownscale}
                         onChange={e => setAlbumSetting(previewFor.title, { allowDownscale: e.target.checked })} />
                  {' '}Réduire davantage si le poids max n'est pas atteint
                </label>
                <span style={{ fontSize: 11, color: 'var(--muted)' }}>
                  (la dimension max s'applique toujours — borne dure. Cette case autorise une réduction
                  supplémentaire des dimensions, par paliers de 15%, jusqu'à 300px minimum, si le poids
                  reste au-dessus du maximum après compression JPEG)
                </span>
                <button onClick={() => resetAlbumSettings(previewFor.title)} style={{ fontSize: 12 }}>
                  Réinitialiser
                </button>
              </div>

              <div style={{ display: 'flex', gap: 24, flex: '1 1 auto', minHeight: 0, overflow: 'hidden' }}>
                <div style={{ flex: 1, display: 'flex', flexDirection: 'column', minHeight: 0 }}>
                  <div style={{ ...muted, flex: '0 0 auto' }}>Avant</div>
                  <div style={{ flex: '1 1 auto', minHeight: 0, display: 'flex', alignItems: 'center', justifyContent: 'center', overflow: 'hidden' }}>
                    <img src={api.coverFullUrl({ ...common, after: false })}
                         alt="avant" style={{ maxWidth: '100%', maxHeight: '100%', objectFit: 'contain', borderRadius: 4 }} />
                  </div>
                  <div style={{ fontSize: 12, color: 'var(--muted)', marginTop: 4, flex: '0 0 auto' }}>
                    {previewFor.cover_format || '?'} · {previewFor.cover_width}×{previewFor.cover_height} · {fmtSize(previewFor.cover_size)}
                  </div>
                </div>
                <div style={{ flex: 1, display: 'flex', flexDirection: 'column', minHeight: 0 }}>
                  <div style={{ ...muted, flex: '0 0 auto' }}>Après</div>
                  <div style={{ flex: '1 1 auto', minHeight: 0, display: 'flex', alignItems: 'center', justifyContent: 'center', overflow: 'hidden' }}>
                    <img src={api.coverFullUrl({ ...common, after: true })}
                         alt="après" style={{ maxWidth: '100%', maxHeight: '100%', objectFit: 'contain', borderRadius: 4 }} />
                  </div>
                  <div style={{ fontSize: 12, color: 'var(--muted)', marginTop: 4, flex: '0 0 auto' }}>
                    {afterInfoLoading && '…'}
                    {afterInfoErr && <span style={{ color: 'var(--danger)' }}>✗ {afterInfoErr}</span>}
                    {afterInfo && !afterInfoLoading && (
                      <>
                        {afterInfo.format} · {afterInfo.width}×{afterInfo.height} · {fmtSize(afterInfo.size)} · qualité {afterInfo.quality}
                        {' · '}
                        {afterInfo.target_met
                          ? <span style={{ color: 'var(--success)' }}>✓ objectif atteint</span>
                          : <span style={{ color: 'var(--warning)' }}>✗ objectif non atteint</span>}
                      </>
                    )}
                  </div>
                </div>
              </div>

              <div style={{ display: 'flex', gap: 8, marginTop: 12, justifyContent: 'flex-end', alignItems: 'center', flex: '0 0 auto' }}>
                {applyMsg[previewFor.title] && <span style={{ fontSize: 12 }}>{applyMsg[previewFor.title]}</span>}
                <button className="btn-primary" disabled={applyBusy === previewFor.title}
                        onClick={() => applyCorrection(previewFor)}>
                  {applyBusy === previewFor.title ? '...' : 'Corriger avec ces réglages'}
                </button>
                <button onClick={closePreview}>Fermer</button>
              </div>
            </div>
          </div>
        )
      })()}
    </div>
  )
}
