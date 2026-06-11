import { useState, useEffect, useRef } from 'react'
import { api, fmtSize, fmtEta, fmtNum } from '../api.js'
import ScanResults from './ScanResults.jsx'
import ScanStats from './ScanStats.jsx'
import ProgressChart from './ProgressChart.jsx'
import { DiskBadge, DiskBar, SameVolumeWarning } from './PathInfo.jsx'

const METHODS = [
  { id:'ultra_fast', label:'Niveau 1 — Ultra-rapide', desc:'Nom + taille uniquement' },
  { id:'fast',       label:'Niveau 2 — Rapide',       desc:'Hash partiel 3×32 Ko (début/milieu/fin, avec cache)' },
  { id:'secure',     label:'Niveau 3 — Complet',      desc:'Hash intégral xxh128' },
  { id:'cloud',      label:'Niveau 4 — Cloud (SHA1 serveur)', desc:'Empreinte SHA1 fournie par pCloud, sans téléchargement — cible pCloud uniquement' },
]


function PathSelector({ label, value, onChange, options, history, role,
                        onValidityChange, bytesToCopy }) {
  const [validity, setValidity] = useState(null)
  const debounceRef = useRef(null)

  useEffect(() => {
    if (!value) { setValidity(null); onValidityChange?.(null); return }
    clearTimeout(debounceRef.current)
    debounceRef.current = setTimeout(() => {
      api.validatePath(value)
        .then(v => { setValidity(v); onValidityChange?.(v) })
        .catch(() => { setValidity(null); onValidityChange?.(null) })
    }, 300)
    return () => clearTimeout(debounceRef.current)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value])

  const histPaths = [...new Set(history.map(h => h[role]).filter(Boolean))]
  const uniqueSuggestions = [...new Set([
    ...(options.disks || []), ...(options.network || []), ...histPaths,
  ])]

  // v3.11 : badge enrichi avec espace disque + projection post-sync pour target
  const projection = (role === 'target' && bytesToCopy > 0) ? bytesToCopy : null
  const badge = <DiskBadge disk={validity?.disk} validity={validity} projection={projection} />

  const roleBadge = role === 'source' ? (
    <span style={{ fontSize:10, padding:'2px 6px', borderRadius:3, background:'#1e3a5f',
                   color:'#93c5fd', textTransform:'uppercase', letterSpacing:'.05em', fontWeight:600 }}>
      🔒 Lecture seule
    </span>
  ) : (
    <span style={{ fontSize:10, padding:'2px 6px', borderRadius:3, background:'#451a1a',
                   color:'#fca5a5', textTransform:'uppercase', letterSpacing:'.05em', fontWeight:600 }}>
      ✏ Écriture
    </span>
  )

  const listId = `paths-${role}`
  return (
    <div style={{ display:'flex', flexDirection:'column', gap:6, minWidth:0 }}>
      <div style={{ display:'flex', justifyContent:'space-between', alignItems:'center', gap:8, flexWrap:'wrap' }}>
        <div style={{ display:'flex', alignItems:'center', gap:8, flexWrap:'wrap' }}>
          <label style={{ margin:0 }}>{label}</label>
          {roleBadge}
        </div>
        {badge}
      </div>
      <input type="text" value={value} list={listId}
        onChange={e => onChange(e.target.value)}
        placeholder="/disks/… ou /network/…"
        style={{ minWidth:0, ...(role === 'source'
          ? { borderLeft:'3px solid #3b82f6' }
          : { borderLeft:'3px solid #ef4444' }) }} />
      <DiskBar disk={validity?.disk} />
      <datalist id={listId}>
        {uniqueSuggestions.map(p => <option key={p} value={p} />)}
      </datalist>
    </div>
  )
}


function ProgressBlock({ status }) {
  const active = !['IDLE','ERROR'].includes(status.app_state)
  const hasSummary = status.new_count || status.different_count ||
                     status.deleted_count || status.identical_count
  if (!active && !hasSummary) return null

  return (
    <div className="card" style={{ marginTop:16 }}>
      {active && (
        <>
          <div style={{ display:'flex', justifyContent:'space-between', marginBottom:4, gap:10, flexWrap:'wrap' }}>
            <span style={{ fontFamily:'var(--mono)', fontSize:12, color:'var(--muted)',
              overflow:'hidden', textOverflow:'ellipsis', whiteSpace:'nowrap', flex:'1 1 200px', minWidth:0 }}>
              {status.current_file || '…'}
            </span>
            <span style={{ color:'var(--muted)', fontSize:12, whiteSpace:'nowrap' }}>
              {status.fps > 0 ? `${status.fps} f/s` : ''}
              {status.eta_seconds > 0 ? ` · ETA ${fmtEta(status.eta_seconds)}` : ''}
            </span>
          </div>
          <div className="progress-bar-track">
            <div className="progress-bar-fill" style={{ width:`${status.progress}%` }} />
          </div>
          <div style={{ textAlign:'right', fontSize:12, color:'var(--muted)' }}>
            {fmtNum(status.processed)} / {fmtNum(status.total)} ({status.progress}%)
          </div>
        </>
      )}
      {hasSummary ? (
        <div style={{ display:'grid', gridTemplateColumns:'repeat(auto-fit, minmax(120px, 1fr))',
                       gap:12, marginTop: active ? 12 : 0 }}>
          {[
            { label:'Nouveaux',   count:status.new_count,       color:'var(--success)' },
            { label:'Modifiés',   count:status.different_count, color:'var(--warning)' },
            { label:'Supprimés',  count:status.deleted_count,   color:'var(--danger)'  },
            { label:'Identiques', count:status.identical_count, color:'var(--muted)'   },
          ].map(s => (
            <div key={s.label} style={{ textAlign:'center', padding:12, background:'var(--bg)', borderRadius:6 }}>
              <div style={{ fontSize:24, fontWeight:700, color:s.color }}>{fmtNum(s.count)}</div>
              <div style={{ fontSize:12, color:'var(--muted)' }}>{s.label}</div>
            </div>
          ))}
        </div>
      ) : null}
      {status.bytes_to_copy > 0 && (
        <div style={{ marginTop:8, fontSize:12, color:'var(--muted)' }}>
          Volume à copier : <strong style={{ color:'var(--text)' }}>{fmtSize(status.bytes_to_copy)}</strong>
        </div>
      )}
      {/* Mini-graphique temps réel (recharts) */}
      {active && <ProgressChart status={status} />}
    </div>
  )
}


function CacheInfo() {
  const [info, setInfo] = useState(null)
  useEffect(() => { api.cacheStats().then(setInfo).catch(() => {}) }, [])
  if (!info) return null
  async function clear() {
    if (!confirm('Vider le cache de hash ? Le prochain scan sera plus lent.')) return
    try { await api.cacheClear(); setInfo({ entries: 0 }) } catch(e) { alert(e.message) }
  }
  return (
    <div style={{ display:'flex', alignItems:'center', gap:10, fontSize:11, color:'var(--muted)', flexWrap:'wrap' }}>
      <span>💾 Cache : {fmtNum(info.entries)} hashs</span>
      <button onClick={clear} style={{
        fontSize:10, padding:'2px 8px', borderRadius:3, background:'var(--bg)',
        border:'1px solid var(--border)', color:'var(--muted)',
        textTransform:'none', letterSpacing:0, cursor:'pointer',
      }}>Vider</button>
    </div>
  )
}


// ── Badges contextuels en haut de la vue post-scan ────────────────────────
function StatusBadges({ status }) {
  const badges = []

  if (status?.source_changed) {
    badges.push(
      <div key="src" style={{
        padding:'10px 14px', background:'#422006', borderLeft:'3px solid var(--warning)',
        borderRadius:6, fontSize:12, color:'#fcd34d',
      }}>
        <strong>⚠ Source modifiée pendant le scan</strong> — {status.source_warning}
      </div>
    )
  }

  if (status?.sync_verified === 'pending') {
    badges.push(
      <div key="pv" style={{
        padding:'10px 14px', background:'#172554', borderLeft:'3px solid var(--accent)',
        borderRadius:6, fontSize:12, color:'#93c5fd',
      }}>
        ⏳ Vérification post-sync en cours…
      </div>
    )
  } else if (status?.sync_verified === 'ok') {
    badges.push(
      <div key="ok" style={{
        padding:'10px 14px', background:'#14532d', borderLeft:'3px solid var(--success)',
        borderRadius:6, fontSize:12, color:'#86efac',
      }}>
        <strong>✓ Sync vérifié</strong> — {status.sync_verified_msg}
      </div>
    )
  } else if (status?.sync_verified === 'failed') {
    badges.push(
      <div key="kf" style={{
        padding:'10px 14px', background:'#450a0a', borderLeft:'3px solid var(--danger)',
        borderRadius:6, fontSize:12, color:'#fca5a5',
      }}>
        <strong>✗ Vérification échouée</strong> — {status.sync_verified_msg}
      </div>
    )
  }

  return badges.length > 0 ? <div style={{ display:'flex', flexDirection:'column', gap:8 }}>{badges}</div> : null
}


export default function TabScanSync({ status }) {
  const [paths,    setPaths]    = useState({ disks:[], network:[] })
  const [history,  setHistory]  = useState([])
  const [source,   setSource]   = useState('')
  const [target,   setTarget]   = useState('')
  const [method,   setMethod]   = useState('fast')
  const [dryRun,   setDryRun]   = useState(true)
  const [filterOn,   setFilterOn]   = useState(false)
  const [filterText, setFilterText] = useState("")
  const [resultsNonce, setResultsNonce] = useState(0)
  const [hideResults, setHideResults] = useState(false)
  const [preview, setPreview] = useState(null)
  const [busy,     setBusy]     = useState(false)
  const [msg,      setMsg]      = useState(null)
  const [view,     setView]     = useState('stats')
  const [pcRoot,   setPcRoot]   = useState('Z:\\GoogleMusic')
  const [repairPrev, setRepairPrev] = useState(null)
  const [busyF4,   setBusyF4]   = useState(false)
  const [profiles,    setProfiles]    = useState([])
  const [profileName, setProfileName] = useState('')
  const [selProfile,  setSelProfile]  = useState('')
  // v3.11 : validités liftées pour comparer source/target (warning même volume)
  const [sourceValidity, setSourceValidity] = useState(null)
  const [targetValidity, setTargetValidity] = useState(null)

  useEffect(() => {
    api.discover().then(setPaths).catch(() => {})
    api.pathsHistory().then(setHistory).catch(() => {})
    api.profiles().then(setProfiles).catch(() => {})
  }, [])

  useEffect(() => {
    if (status?.source && !source) setSource(status.source)
    if (status?.target && !target) setTarget(status.target)
    if (status?.method) setMethod(status.method)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status?.source, status?.target, status?.method])

  const prevState = useRef(status?.app_state)
  useEffect(() => {
    if (prevState.current && prevState.current !== 'IDLE' && status?.app_state === 'IDLE') {
      api.pathsHistory().then(setHistory).catch(() => {})
      setResultsNonce(x => x + 1)
    }
    prevState.current = status?.app_state
  }, [status?.app_state])

  useEffect(() => {
    if (!filterOn || !filterText.trim() || !source) { setPreview(null); setHideResults(false); return }
    setHideResults(true)
    const t = setTimeout(() => {
      api.scanDirs({ source, filter: filterText.trim() }).then(setPreview).catch(() => setPreview(null))
    }, 400)
    return () => clearTimeout(t)
  }, [filterOn, filterText, source])

  const isActive = status && !['IDLE','ERROR'].includes(status.app_state)
  const hasDiff  = status && (status.new_count || status.different_count || status.deleted_count)
  const scanDone = status?.scan_done

  const notify = (text, ok=true) => { setMsg({ text, ok }); setTimeout(() => setMsg(null), 5000) }

  async function doScan() {
    if (!source || !target) return notify('Choisissez source et cible', false)
    if (filterOn && filterText.trim()) {
      try { const r = await api.scanDirs({ source, filter: filterText.trim() }); if (!r || !r.count) { setHideResults(true); return notify("Aucun dossier ne correspond au filtre : " + filterText.trim(), false) } } catch(e) {}
    }
    try { setBusy(true); setHideResults(false); await api.scan({ source, target, method, filter: filterOn ? filterText : "" }); notify('Scan démarré') }
    catch(e) { notify(e.message, false) } finally { setBusy(false) }
  }

  async function doRepairPreview() {
    try { setBusyF4(true); const r = await api.repairPreview(pcRoot); setRepairPrev(r) }
    catch(e) { notify(e.message, false) } finally { setBusyF4(false) }
  }

  async function doSync() {
    if (!confirm(dryRun
        ? 'Lancer la SIMULATION (aucune écriture) ?'
        : '⚠ Lancer la SYNCHRONISATION RÉELLE ?\n\nLa source ne sera pas touchée.\n' +
          'Une vérification automatique sera lancée après le sync.')) return
    try { setBusy(true); await api.sync({ dry_run: dryRun })
      notify(dryRun ? 'Simulation démarrée' : 'Synchronisation démarrée') }
    catch(e) { notify(e.message, false) } finally { setBusy(false) }
  }

  async function doAbort() {
    try { await api.abort(); notify('Arrêt demandé — peut prendre quelques secondes') }
    catch(e) { notify(e.message, false) }
  }
  async function doReset() {
    try { await api.reset(); notify('Réinitialisé') } catch(e) { notify(e.message, false) }
  }
  function pickHistory(e) { setSource(e.source); setTarget(e.target) }
  function applyProfile(p) {
    if (!p) return
    setSource(p.source || '')
    setTarget(p.target || '')
    if (p.method) setMethod(p.method)
  }
  async function saveProfile() {
    const name = profileName.trim()
    if (!name) return notify('Donnez un nom au profil', false)
    if (!source || !target) return notify('Source et cible requises', false)
    try {
      const list = await api.profileSave({ name, source, target, method })
      setProfiles(list); setProfileName(''); setSelProfile(name)
      notify('Profil enregistré')
    } catch(e) { notify(e.message, false) }
  }
  async function deleteProfile(name) {
    if (!name) return
    if (!confirm(`Supprimer le profil « ${name} » ?`)) return
    try {
      const list = await api.profileDelete(name)
      setProfiles(list); if (selProfile === name) setSelProfile('')
      notify('Profil supprimé')
    } catch(e) { notify(e.message, false) }
  }

  return (
    <div style={{ display:'flex', flexDirection:'column', gap:16 }}>
      {msg && (
        <div style={{
          padding:'10px 16px', borderRadius:'var(--radius)',
          background: msg.ok ? '#14532d' : '#450a0a',
          color: msg.ok ? 'var(--success)' : 'var(--danger)',
        }}>{msg.text}</div>
      )}

      {status?.app_state === 'ERROR' && (
        <div style={{ padding:'12px 16px', background:'#450a0a', borderRadius:'var(--radius)', color:'var(--danger)' }}>
          <strong>ERREUR</strong> — {status.error}
          <button className="btn-ghost" onClick={doReset} style={{ marginLeft:12, fontSize:12, padding:'4px 10px' }}>Réinitialiser</button>
        </div>
      )}

      {/* Badges contextuels (source modifiée, sync vérifié, etc.) */}
      <StatusBadges status={status} />

      {!isActive && (
        <div className="card">
          <h3 style={{ marginBottom:12, fontSize:14 }}>Profils de synchro</h3>
          {profiles.length > 0 && (
            <div style={{ display:'flex', gap:8, alignItems:'center', flexWrap:'wrap', marginBottom:10 }}>
              <select value={selProfile}
                onChange={e => { const p = profiles.find(x => x.name === e.target.value); setSelProfile(e.target.value); if (p) applyProfile(p) }}
                style={{ flex:'1 1 220px', minWidth:0 }}>
                <option value="">📁 Charger un profil… ({profiles.length})</option>
                {profiles.map(p => <option key={p.name} value={p.name}>{p.name} — {p.source} → {p.target}</option>)}
              </select>
              <button className="btn-ghost" disabled={!selProfile}
                onClick={() => deleteProfile(selProfile)} style={{ fontSize:12, padding:'6px 10px' }}>
                🗑 Supprimer
              </button>
            </div>
          )}
          <div style={{ display:'flex', gap:8, alignItems:'center', flexWrap:'wrap' }}>
            <input type="text" value={profileName} onChange={e => setProfileName(e.target.value)}
              placeholder="Nom du profil à enregistrer"
              style={{ flex:'1 1 220px', minWidth:0 }} />
            <button className="btn-ghost" disabled={!profileName || !source || !target}
              onClick={saveProfile} style={{ fontSize:12, padding:'6px 10px' }}>
              💾 Enregistrer le profil actuel
            </button>
          </div>
        </div>
      )}

      {history.length > 0 && !isActive && (
        <div className="card">
          <h3 style={{ marginBottom:12, fontSize:14 }}>Récents</h3>
          <div style={{ display:'flex', flexDirection:'column', gap:6 }}>
            {history.slice(0, 5).map((h, i) => {
              const so = h.source_status?.exists && h.source_status?.is_dir
              const to = h.target_status?.exists && h.target_status?.is_dir
              return (
                <button key={i} onClick={() => pickHistory(h)} style={{
                  display:'flex', alignItems:'center', justifyContent:'space-between',
                  padding:'8px 12px', background:'var(--bg)', border:'1px solid var(--border)',
                  borderRadius:6, textTransform:'none', letterSpacing:0, cursor:'pointer',
                  fontSize:12, color:'var(--text)', textAlign:'left',
                }}>
                  <span style={{ fontFamily:'var(--mono)', flex:1, overflow:'hidden',
                                 textOverflow:'ellipsis', whiteSpace:'nowrap' }}>
                    <span style={{ color: so ? 'var(--success)' : 'var(--danger)' }}>{so ? '✓' : '✗'}</span> {h.source}
                    <span style={{ color:'var(--muted)' }}> → </span>
                    <span style={{ color: to ? 'var(--success)' : 'var(--danger)' }}>{to ? '✓' : '✗'}</span> {h.target}
                  </span>
                </button>
              )
            })}
          </div>
        </div>
      )}

      <div className="card">
        <h3 style={{ marginBottom:16, fontSize:14 }}>Chemins</h3>
        <div style={{ display:'grid', gridTemplateColumns:'repeat(auto-fit, minmax(260px, 1fr))', gap:16 }}>
          <PathSelector label="Source" value={source} onChange={setSource}
                        options={paths} history={history} role="source"
                        onValidityChange={setSourceValidity} />
          <PathSelector label="Cible"  value={target} onChange={setTarget}
                        options={paths} history={history} role="target"
                        onValidityChange={setTargetValidity}
                        bytesToCopy={status?.bytes_to_copy} />
        </div>
        <SameVolumeWarning sourceDisk={sourceValidity?.disk} targetDisk={targetValidity?.disk} />
        <div style={{ marginTop:12, padding:'8px 12px', background:'var(--bg)', borderRadius:6,
                      fontSize:11, color:'var(--muted)', borderLeft:'3px solid var(--accent)' }}>
          ℹ️ Le sens est strict : <strong>source → cible</strong>. La source n'est jamais modifiée.
        </div>
      </div>

      <div className="card">
        <h3 style={{ marginBottom:12, fontSize:14 }}>Niveau de précision</h3>
        <div style={{ display:'flex', flexDirection:'column', gap:8 }}>
          {METHODS.map(m => (
            <label key={m.id} style={{
              display:'flex', alignItems:'center', gap:10, cursor:'pointer',
              padding:'10px 14px', borderRadius:6,
              background: method===m.id ? '#1e3a5f' : 'var(--bg)',
              border: `1px solid ${method===m.id ? 'var(--accent)' : 'var(--border)'}`,
              textTransform:'none', letterSpacing:0,
            }}>
              <input type="radio" name="method" value={m.id}
                checked={method===m.id} onChange={() => setMethod(m.id)}
                style={{ accentColor:'var(--accent)' }} />
              <div>
                <div style={{ fontWeight:600, fontSize:13, color:'var(--text)' }}>{m.label}</div>
                <div style={{ fontSize:12, color:'var(--muted)' }}>{m.desc}</div>
              </div>
            </label>
          ))}
        </div>
      </div>

      <div className="card">
        <div style={{ display:'flex', justifyContent:'space-between', alignItems:'center',
                       marginBottom:12, flexWrap:'wrap', gap:8 }}>
          <h3 style={{ fontSize:14, margin:0 }}>Étape 1 — Analyser</h3>
          <CacheInfo />
        </div>
        <div style={{ display:'flex', gap:10, alignItems:'center', flexWrap:'wrap' }}>
          <div style={{ display:"flex", gap:10, alignItems:"center", flexWrap:"wrap", marginBottom:12, flexBasis:"100%" }}>
            <label style={{ display:"flex", alignItems:"center", gap:6, fontSize:13, cursor:"pointer" }}>
              <input type="checkbox" checked={filterOn} onChange={e => setFilterOn(e.target.checked)} style={{ accentColor:"var(--accent)" }} />
              🔎 Filtrer par nom de dossier
            </label>
            <input type="text" value={filterText} onChange={e => setFilterText(e.target.value)} placeholder="ex : willy" disabled={!filterOn} style={{ flex:1, minWidth:160, padding:"6px 10px", background:"var(--bg)", border:"1px solid var(--border)", borderRadius:6, color:"var(--text)", opacity: filterOn ? 1 : 0.5 }} />
          </div>
          {filterOn && filterText.trim() && preview && (
            <div style={{ marginBottom:12, fontSize:13, flexBasis:"100%" }}>
              {preview.count === 0 ? (
                <div style={{ color:"var(--danger)", padding:"8px 12px", background:"var(--bg)", borderRadius:6, border:"1px solid var(--danger)" }}>Aucun dossier ne correspond au filtre : {filterText.trim()}</div>
              ) : (
                <div>
                  <div style={{ color:"var(--success)", marginBottom:6 }}>🔎 Périmètre : {preview.count} dossier(s) / {preview.total_files} fichier(s)</div>
                  <div style={{ maxHeight:180, overflowY:"auto", border:"1px solid var(--border)", borderRadius:6 }}>
                    {preview.dirs.map(d => (
                      <div key={d.name} style={{ display:"flex", justifyContent:"space-between", padding:"4px 10px", borderBottom:"1px solid var(--border)", fontSize:12 }}>
                        <span>{d.name}</span><span style={{ color:"var(--muted)" }}>{d.total}</span>
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>
          )}
          {!isActive ? (
            <button className="btn-primary" onClick={doScan} disabled={busy || !source || !target}>
              🔍 Lancer le scan
            </button>
          ) : (
            <button className="btn-danger" onClick={doAbort} style={{ fontSize:14, padding:'10px 20px' }}>
              ⏹ ARRÊTER ({status?.app_state})
            </button>
          )}
          <span style={{ flex:1 }} />
          <button className="btn-ghost" onClick={() => api.discover().then(setPaths).catch(()=>{})}>
            ↻ Actualiser
          </button>
        </div>
      </div>

      {status && !hideResults && <ProgressBlock status={status} />}

      {scanDone && !isActive && !hideResults && (
        <>
          <div className="card">
            <div style={{ display:'flex', justifyContent:'space-between', alignItems:'center',
                           marginBottom:12, flexWrap:'wrap', gap:8 }}>
              <h3 style={{ fontSize:14, margin:0 }}>Résultat du scan</h3>
              <div style={{ display:'flex', gap:4, flexWrap:'wrap' }}>
                {[
                  { id:'stats',   label:'📊 Statistiques' },
                  { id:'details', label:'📋 Détail' },
                  { id:'none',    label:'⊟ Masquer' },
                ].map(t => (
                  <button key={t.id} onClick={() => setView(t.id)} style={{
                    fontSize:11, padding:'5px 10px', borderRadius:4,
                    background: view === t.id ? 'var(--accent)' : 'var(--bg)',
                    color:      view === t.id ? '#fff' : 'var(--muted)',
                    border:     `1px solid ${view === t.id ? 'var(--accent)' : 'var(--border)'}`,
                    textTransform:'none', letterSpacing:0, cursor:'pointer',
                  }}>{t.label}</button>
                ))}
              </div>
            </div>
            {view === 'stats'   && <ScanStats key={status?.scan_seq} status={status} />}
            {view === 'details' && <ScanResults key={status?.scan_seq} />}
          </div>

          <div className="card">
            <h3 style={{ marginBottom:12, fontSize:14 }}>Étape 2 — Synchroniser</h3>
            {!hasDiff ? (
              <div style={{ padding:12, background:'var(--bg)', borderRadius:6, color:'var(--muted)', fontSize:13 }}>
                ✓ Source et cible sont déjà identiques — rien à synchroniser.
              </div>
            ) : (
              <div style={{ display:'flex', gap:12, alignItems:'center', flexWrap:'wrap' }}>
                <label style={{
                  display:'flex', alignItems:'center', gap:6,
                  textTransform:'none', letterSpacing:0, cursor:'pointer',
                  color:'var(--text)', fontSize:13,
                }}>
                  <input type="checkbox" checked={dryRun} onChange={e => setDryRun(e.target.checked)}
                    style={{ accentColor:'var(--accent)' }} />
                  Simulation (aucune écriture)
                </label>
                <button className={dryRun ? 'btn-ghost' : 'btn-success'} onClick={doSync} disabled={busy}>
                  {dryRun ? '🧪 Simuler la synchronisation' : '⚡ Synchroniser pour de vrai'}
                </button>
              </div>
            )}
          </div>

          <div className="card">
            <h3 style={{ marginBottom:12, fontSize:14 }}>Réparation pochettes — playlist EZ CD</h3>
            <div style={{ fontSize:12, color:'var(--muted)', marginBottom:12 }}>
              Génère un <strong>.m3u8</strong> (UTF-8 BOM, pour EZ CD) listant <strong>toutes</strong> les pistes
              des albums contenant un fichier en écart (<code>read_error</code> / <code>content</code>) du
              dernier scan. ⚠ Scan <strong>fast</strong> ou <strong>secure</strong> requis — le mode <em>cloud</em>
              ne détecte pas ces fichiers.
            </div>
            <div style={{ display:'flex', gap:10, alignItems:'center', flexWrap:'wrap' }}>
              <label style={{ fontSize:12, color:'var(--muted)' }}>Racine PC (SMB) :</label>
              <input type="text" value={pcRoot} onChange={e => setPcRoot(e.target.value)}
                placeholder="Z:\GoogleMusic"
                style={{ minWidth:200, fontFamily:'var(--mono)' }} />
              <button className="btn-ghost" onClick={doRepairPreview} disabled={busyF4 || !pcRoot}>
                🔍 Aperçu
              </button>
              {repairPrev && repairPrev.track_count > 0 && (
                <a className="btn-primary" href={api.repairUrl(pcRoot)} style={{ textDecoration:'none' }}>
                  ⬇ Télécharger ({repairPrev.album_count} alb. / {repairPrev.track_count} pistes)
                </a>
              )}
            </div>
            {repairPrev && (
              <div style={{ marginTop:10, fontSize:12, color: repairPrev.track_count > 0 ? 'var(--warning)' : 'var(--muted)' }}>
                {repairPrev.track_count > 0
                  ? `${repairPrev.album_count} album(s) à réparer, ${repairPrev.track_count} piste(s).`
                  : 'Aucun album à réparer dans le dernier scan.'}
              </div>
            )}
          </div>
        </>
      )}
    </div>
  )
}
