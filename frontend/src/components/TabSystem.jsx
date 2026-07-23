import { useState, useEffect, useRef } from 'react'
import { api, openWsLogs, fmtSize, fmtDateDistance, copyToClipboard, downloadJson } from '../api.js'
import SmartPanel from './SmartPanel.jsx'
import IgnorePanel from './IgnorePanel.jsx'  // v3.12
import pkg from '../../package.json'
import lock from '../../package-lock.json'


function LogConsole() {
  const [lines,   setLines]   = useState([])
  const [paused,  setPaused]  = useState(false)
  const [filter,  setFilter]  = useState('')
  const [level,   setLevel]   = useState('ALL')
  const bottomRef = useRef(null); const wsRef = useRef(null); const pausedRef = useRef(false)

  useEffect(() => {
    api.logsRecent(300).then(r => setLines(r.lines)).catch(() => {})
    wsRef.current = openWsLogs(line => {
      if (!pausedRef.current) setLines(prev => [...prev.slice(-1999), line])
    })
    return () => wsRef.current?.close()
  }, [])

  useEffect(() => {
    pausedRef.current = paused
    if (!paused && bottomRef.current) bottomRef.current.scrollIntoView({ behavior:'smooth' })
  }, [lines, paused])

  const LEVEL_COLORS = {
    DEBUG: 'var(--muted)', INFO: 'var(--accent)',
    WARN: 'var(--warning)', WARNING: 'var(--warning)', ERROR: 'var(--danger)',
  }
  const filtered = lines.filter(l => {
    if (filter && !l.toLowerCase().includes(filter.toLowerCase())) return false
    if (level !== 'ALL') return l.includes(`[${level}`) || l.includes(`[${level.toLowerCase()}`)
    return true
  })
  const lineColor = l => {
    for (const [k, v] of Object.entries(LEVEL_COLORS))
      if (l.includes(`[${k}`)) return v
    return 'var(--text)'
  }

  return (
    <div className="card" style={{ padding:0, overflow:'hidden' }}>
      <div style={{
        display:'flex', alignItems:'center', gap:8, padding:'10px 14px',
        borderBottom:'1px solid var(--border)', flexWrap:'wrap',
      }}>
        <span style={{ fontWeight:600, fontSize:13 }}>Console logs</span>
        <span style={{ flex:1 }} />
        <select value={level} onChange={e => setLevel(e.target.value)} style={{ width:'auto', padding:'4px 8px', fontSize:12 }}>
          {['ALL','DEBUG','INFO','WARNING','ERROR'].map(l => <option key={l}>{l}</option>)}
        </select>
        <input type="text" placeholder="Filtrer…" value={filter}
          onChange={e => setFilter(e.target.value)} style={{ width:160, padding:'4px 8px', fontSize:12 }} />
        <button className="btn-ghost" onClick={() => setPaused(p => !p)} style={{ fontSize:12, padding:'4px 10px' }}>
          {paused ? '▶ Reprendre' : '⏸ Pause'}
        </button>
        <button className="btn-ghost" onClick={() => setLines([])} style={{ fontSize:12, padding:'4px 10px' }}>
          🗑 Vider
        </button>
      </div>
      <div style={{
        height:400, overflowY:'auto', background:'#0a0c12',
        fontFamily:'var(--mono)', fontSize:11, padding:'8px 12px',
        display:'flex', flexDirection:'column', gap:1,
      }}>
        {filtered.length === 0 ? (
          <span style={{ color:'var(--muted)', padding:8 }}>En attente de logs…</span>
        ) : filtered.map((l, i) => (
          <div key={i} style={{ color: lineColor(l), wordBreak:'break-all', lineHeight:1.5 }}>{l}</div>
        ))}
        <div ref={bottomRef} />
      </div>
    </div>
  )
}


function BumpBadge({ bump }) {
  if (!bump) return null
  const styles = {
    patch: { bg:'#064e3b', color:'#6ee7b7', label:'PATCH' },
    minor: { bg:'#422006', color:'#fcd34d', label:'MINOR' },
    major: { bg:'#7c2d12', color:'#fdba74', label:'MAJOR' },
  }
  const s = styles[bump] || { bg:'var(--bg)', color:'var(--muted)', label:bump.toUpperCase() }
  return (
    <span style={{ fontSize: 10, padding: '2px 6px', borderRadius: 3, marginLeft: 6,
      background: s.bg, color: s.color, fontWeight: 700, letterSpacing: '.05em' }}>{s.label}</span>
  )
}


function DateInfo({ installedDate, latestDate }) {
  if (!installedDate && !latestDate) return null
  const today = new Date().toISOString().slice(0, 10)
  return (
    <div style={{ fontSize: 10, color: 'var(--muted)', marginTop: 2, lineHeight: 1.4 }}>
      {installedDate && <div>installée : {installedDate}{latestDate && installedDate !== latestDate ? ` (il y a ${fmtDateDistance(installedDate, today)})` : ''}</div>}
      {latestDate && installedDate !== latestDate && <div>dernière : {latestDate}</div>}
      {installedDate && latestDate && installedDate !== latestDate && (
        <div style={{ color: '#fb923c' }}>écart : {fmtDateDistance(installedDate, latestDate)}</div>
      )}
    </div>
  )
}


function ExportButtons({ getData, filename }) {
  const [copied, setCopied] = useState(false)
  return (
    <div style={{ display:'flex', gap:4 }}>
      <button className="btn-ghost"
        onClick={async () => {
          const ok = await copyToClipboard(JSON.stringify(getData(), null, 2))
          if (ok) { setCopied(true); setTimeout(() => setCopied(false), 2000) }
        }}
        style={{ fontSize:11, padding:'4px 10px' }}>
        {copied ? '✓ Copié' : '📋 Copier JSON'}
      </button>
      <button className="btn-ghost"
        onClick={() => downloadJson(getData(), filename)}
        style={{ fontSize:11, padding:'4px 10px' }}>⬇ Télécharger</button>
    </div>
  )
}


function semverCompare(a, b) {
  if (!a || !b || a === 'N/A' || b === 'N/A') return 0
  const pa = a.replace(/^[~^]/, '').split('.').map(n => parseInt(n) || 0)
  const pb = b.replace(/^[~^]/, '').split('.').map(n => parseInt(n) || 0)
  for (let i = 0; i < 3; i++) {
    const da = pa[i] || 0, db = pb[i] || 0
    if (da < db) return -1
    if (da > db) return 1
  }
  return 0
}
function cleanVer(v) { return (v || '').replace(/^[~^]/, '') }
function bumpType(installed, latest) {
  if (!installed || !latest || latest === 'N/A') return null
  const pa = cleanVer(installed).split('.').map(n => parseInt(n) || 0)
  const pb = cleanVer(latest).split('.').map(n => parseInt(n) || 0)
  if ((pb[0] || 0) > (pa[0] || 0)) return 'major'
  if ((pb[1] || 0) > (pa[1] || 0)) return 'minor'
  if ((pb[2] || 0) > (pa[2] || 0)) return 'patch'
  return null
}


function DepsTablePython() {
  const [deps,     setDeps]     = useState(null)
  const [checking, setChecking] = useState(false)
  useEffect(() => { api.dependencies().then(setDeps).catch(() => {}) }, [])
  const checkUpdates = async () => {
    setChecking(true)
    try { setDeps(await api.checkUpdates()) } catch {}
    finally { setChecking(false) }
  }
  if (!deps) return <div style={{ color:'var(--muted)', padding:16 }}>Chargement…</div>
  return (
    <div className="card" style={{ padding:0, overflow:'hidden' }}>
      <div style={{ display:'flex', alignItems:'center', gap:8,
                     padding:'10px 14px', borderBottom:'1px solid var(--border)', flexWrap:'wrap' }}>
        <span style={{ fontWeight:600, fontSize:13 }}>🐍 Dépendances Python (backend)</span>
        <span style={{ flex:1 }} />
        <ExportButtons getData={() => ({ generated_at: new Date().toISOString(), deps })}
                        filename="python-deps.json" />
        <button className="btn-ghost" onClick={checkUpdates} disabled={checking}
                style={{ fontSize:11, padding:'4px 10px' }}>
          {checking ? '⟳ Vérification…' : '🔍 Vérifier les MAJ'}
        </button>
      </div>
      <div style={{ overflowX:'auto' }}>
        <table style={{ minWidth:560 }}>
          <thead><tr><th>Paquet</th><th>Installé</th><th>Dernière version</th><th>Statut</th></tr></thead>
          <tbody>
            {deps.map(d => (
              <tr key={d.name}>
                <td className="mono" style={{ verticalAlign:'top' }}>{d.name}</td>
                <td className="mono" style={{ color:'var(--muted)', verticalAlign:'top' }}>
                  {d.installed}
                  {d.installed_date && <div style={{ fontSize:10 }}>{d.installed_date}</div>}
                </td>
                <td className="mono" style={{ color:'var(--muted)', verticalAlign:'top' }}>
                  {d.latest ?? '—'}
                  {d.latest_date && <div style={{ fontSize:10 }}>{d.latest_date}</div>}
                </td>
                <td style={{ verticalAlign:'top' }}>
                  {d.up_to_date === null ? <span className="badge badge-gray">?</span>
                    : d.up_to_date ? <span className="badge badge-green">✓ À jour</span>
                    : <>
                        <span className="badge badge-yellow">↑ {d.latest}</span>
                        <BumpBadge bump={d.bump} />
                        <DateInfo installedDate={d.installed_date} latestDate={d.latest_date} />
                        <div style={{ marginTop:4 }}>
                          <a href={`https://pypi.org/project/${d.name}/${d.latest}/`}
                             target="_blank" rel="noreferrer"
                             style={{ fontSize:11, color:'var(--accent)' }}>changelog</a>
                        </div>
                      </>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}


function DepsTableNpm() {
  const initial = []
  for (const [name, version] of Object.entries(pkg.dependencies || {}))
    initial.push({ name, declared: version,
      installed: lock.packages?.['node_modules/' + name]?.version || cleanVer(version), kind: 'prod' })
  for (const [name, version] of Object.entries(pkg.devDependencies || {}))
    initial.push({ name, declared: version,
      installed: lock.packages?.['node_modules/' + name]?.version || cleanVer(version), kind: 'dev'  })
  initial.sort((a, b) => a.kind !== b.kind ? (a.kind === 'prod' ? -1 : 1) : a.name.localeCompare(b.name))

  const [deps, setDeps] = useState(initial.map(d => ({
    ...d, latest: null, installed_date: null, latest_date: null, bump: null, up_to_date: null })))
  const [checking, setChecking] = useState(false)
  const [audit,    setAudit]    = useState(null)
  const [auditing, setAuditing] = useState(false)

  const checkUpdates = async () => {
    setChecking(true)
    try {
      const updated = await Promise.all(deps.map(async d => {
        try {
          const r = await api.npmInfo(d.name, d.installed)
          const latest = r.latest
          const up_to_date = latest === 'N/A' ? null : semverCompare(cleanVer(d.installed), latest) >= 0
          return { ...d, latest, installed_date: r.installed_date, latest_date: r.latest_date,
            bump: r.bump || bumpType(d.installed, latest), up_to_date }
        } catch { return { ...d, latest: 'N/A', up_to_date: null } }
      }))
      setDeps(updated)
    } finally { setChecking(false) }
  }

  const runAudit = async () => {
    setAuditing(true)
    try {
      const depMap = {}
      deps.forEach(d => { depMap[d.name] = cleanVer(d.installed) })
      setAudit(await api.npmAudit(depMap))
    } catch(e) {
      setAudit({ status: 'error', error: e.message, advisories: {} })
    } finally { setAuditing(false) }
  }

  const severityCount = audit && audit.advisories
    ? Object.values(audit.advisories).flat().reduce((acc, a) => {
        acc[a.severity] = (acc[a.severity] || 0) + 1; return acc }, {}) : null

  const exportData = () => ({ generated_at: new Date().toISOString(),
    frontend_version: pkg.version, deps, audit })

  return (
    <div className="card" style={{ padding:0, overflow:'hidden' }}>
      <div style={{ display:'flex', alignItems:'center', gap:8,
                     padding:'10px 14px', borderBottom:'1px solid var(--border)', flexWrap:'wrap' }}>
        <span style={{ fontWeight:600, fontSize:13 }}>
          📦 Dépendances npm (frontend) — version {pkg.version}
        </span>
        <span style={{ flex:1 }} />
        <ExportButtons getData={exportData} filename="npm-deps-audit.json" />
        <button className="btn-ghost" onClick={runAudit} disabled={auditing}
                style={{ fontSize:11, padding:'4px 10px' }}>
          {auditing ? '⟳ Audit…' : '🛡 Audit sécurité'}
        </button>
        <button className="btn-ghost" onClick={checkUpdates} disabled={checking}
                style={{ fontSize:11, padding:'4px 10px' }}>
          {checking ? '⟳ Vérification…' : '🔍 Vérifier les MAJ'}
        </button>
      </div>

      {audit && (
        <div style={{ padding:'10px 14px', borderBottom:'1px solid var(--border)',
                       background: audit.total > 0 ? '#422006' : '#14532d',
                       color: audit.total > 0 ? '#fcd34d' : '#86efac', fontSize:12 }}>
          {audit.status === 'error' ? (
            <>⚠ Échec de l'audit : {audit.error || 'erreur inconnue'}</>
          ) : audit.total === 0 ? (
            <>✓ Aucune vulnérabilité connue dans tes dépendances npm.</>
          ) : (
            <>⚠ <strong>{audit.total} vulnérabilité(s) trouvée(s)</strong>
              {severityCount && Object.keys(severityCount).length > 0 && (
                <> · {Object.entries(severityCount).map(([s, c]) => `${s}: ${c}`).join(', ')}</>
              )}
            </>
          )}
        </div>
      )}

      <div style={{ overflowX:'auto' }}>
        <table style={{ minWidth:600 }}>
          <thead><tr><th>Paquet</th><th>Type</th><th>Installée</th><th>Dernière version</th><th>Statut</th></tr></thead>
          <tbody>
            {deps.map(d => {
              const advs = audit?.advisories?.[d.name] || []
              return (
                <tr key={d.name}>
                  <td className="mono" style={{ verticalAlign:'top' }}>{d.name}</td>
                  <td style={{ verticalAlign:'top' }}>
                    <span className={d.kind === 'prod' ? 'badge badge-green' : 'badge badge-gray'}
                          style={{ fontSize:10 }}>{d.kind === 'prod' ? 'prod' : 'dev'}</span>
                  </td>
                  <td className="mono" style={{ color:'var(--muted)', verticalAlign:'top' }}>
                    {d.installed}
                    {d.installed_date && <div style={{ fontSize:10 }}>{d.installed_date}</div>}
                    {d.declared && <div style={{ fontSize:10, color:'var(--muted)' }}>{d.declared}</div>}
                  </td>
                  <td className="mono" style={{ color:'var(--muted)', verticalAlign:'top' }}>
                    {d.latest ?? '—'}
                    {d.latest_date && <div style={{ fontSize:10 }}>{d.latest_date}</div>}
                  </td>
                  <td style={{ verticalAlign:'top' }}>
                    {d.up_to_date === null ? <span className="badge badge-gray">?</span>
                      : d.up_to_date ? <span className="badge badge-green">✓ À jour</span>
                      : <>
                          <span className="badge badge-yellow">↑ {d.latest}</span>
                          <BumpBadge bump={d.bump} />
                          <DateInfo installedDate={d.installed_date} latestDate={d.latest_date} />
                          <div style={{ marginTop:4 }}>
                            <a href={`https://www.npmjs.com/package/${d.name}/v/${d.latest}`}
                               target="_blank" rel="noreferrer"
                               style={{ fontSize:11, color:'var(--accent)' }}>page npm</a>
                          </div>
                        </>}
                    {advs.length > 0 && <Vulnerabilities advs={advs} />}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}


function Vulnerabilities({ advs }) {
  const SEV_COLOR = { critical: '#dc2626', high: '#ef4444', moderate: '#f59e0b',
                       low: '#fbbf24', info: '#9ca3af' }
  return (
    <div style={{ marginTop:6, padding:'6px 8px', background:'#1f1f1f',
                  borderLeft:'2px solid var(--danger)', borderRadius:4, fontSize:11 }}>
      <div style={{ fontWeight:600, color:'var(--danger)', marginBottom:4 }}>
        🛡 {advs.length} CVE{advs.length > 1 ? 's' : ''}
      </div>
      {advs.map((a, i) => (
        <div key={i} style={{ marginTop:2, lineHeight:1.3 }}>
          <span style={{ color: SEV_COLOR[a.severity] || 'var(--muted)',
                          fontWeight:600, marginRight:6 }}>[{a.severity}]</span>
          <a href={a.url} target="_blank" rel="noreferrer"
             style={{ color:'var(--text)', textDecoration:'none' }}>{a.title}</a>
          <div style={{ color:'var(--muted)', fontSize:10 }}>
            versions vulnérables : {a.vulnerable}
          </div>
        </div>
      ))}
    </div>
  )
}


function ContextExportPanel() {
  const [loading, setLoading] = useState(false)
  const [data,    setData]    = useState(null)

  const load = async () => {
    setLoading(true)
    try { setData(await api.exportContext()) } catch {}
    finally { setLoading(false) }
  }
  useEffect(() => { load() }, [])

  return (
    <div className="card" style={{ padding:0, overflow:'hidden' }}>
      <div style={{ display:'flex', alignItems:'center', gap:8,
                     padding:'10px 14px', borderBottom:'1px solid var(--border)', flexWrap:'wrap' }}>
        <span style={{ fontWeight:600, fontSize:13 }}>
          🧬 Export de contexte (pour reprise de conversation)
        </span>
        <span style={{ flex:1 }} />
        {data && <ExportButtons getData={() => data}
          filename={`zimacompare-context-${new Date().toISOString().slice(0,10)}.json`} />}
        <button className="btn-ghost" onClick={load} disabled={loading}
                style={{ fontSize:11, padding:'4px 10px' }}>
          {loading ? '⟳ Chargement…' : '↻ Actualiser'}
        </button>
      </div>
      <div style={{ padding:14 }}>
        <p style={{ fontSize:12, color:'var(--muted)', marginBottom:10, lineHeight:1.5 }}>
          Snapshot complet de l'application : versions, état, config, historique,
          SMART des disques, logs récents.
        </p>
        <div style={{ padding:'10px 12px', background:'var(--bg)', borderRadius:6,
                       fontSize:11, color:'var(--muted)', borderLeft:'3px solid var(--accent)' }}>
          <strong style={{ color:'var(--text)' }}>Pour reprendre une nouvelle conversation, partage :</strong>
          <ol style={{ margin:'6px 0 0 18px', padding:0 }}>
            <li>Ce fichier <code>zimacompare-context-*.json</code></li>
          </ol>
        </div>
        {data && (
          <div style={{ marginTop:12, fontSize:11, color:'var(--muted)' }}>
            <strong style={{ color:'var(--text)' }}>Résumé :</strong>{' '}
            schéma v{data.schema_version} · ZimaCompare {data.app_version} ·{' '}
            {Object.keys(data.python_deps || {}).length} deps Python ·{' '}
            {Object.keys(data.npm_declared?.prod || {}).length + Object.keys(data.npm_declared?.dev || {}).length} deps npm ·{' '}
            {data.paths_history?.length || 0} entrées historique ·{' '}
            {Object.keys(data.data_files || {}).length} fichiers data ·{' '}
            {data.smart?.length || 0} disques · cache : {data.cache?.entries || 0} hashs
          </div>
        )}
      </div>
    </div>
  )
}


export default function TabSystem() {
  return (
    <div style={{ display:'flex', flexDirection:'column', gap:20 }}>
      <SmartPanel />
      <IgnorePanel />
      <LogConsole />
      <DepsTablePython />
      <DepsTableNpm />
      <ContextExportPanel />
    </div>
  )
}
