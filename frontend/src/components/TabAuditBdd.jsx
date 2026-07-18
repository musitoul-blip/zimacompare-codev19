import { useState, useEffect, useCallback } from 'react'
import { api } from '../api.js'

const STATUS = {
  ok:   { color:'#22c55e', icon:'●', label:'OK' },
  warn: { color:'#f59e0b', icon:'▲', label:'A VOIR' },
  fail: { color:'#ef4444', icon:'✗', label:'ECHEC' },
}

function Badge({ status }) {
  const s = STATUS[status] || STATUS.warn
  return (
    <span style={{ fontSize:11, fontWeight:600, padding:'2px 8px', borderRadius:4,
      background:s.color + '22', color:s.color, whiteSpace:'nowrap' }}>
      {s.icon} {s.label}
    </span>
  )
}

export default function TabAuditBdd() {
  const [data, setData] = useState(null)
  const [busy, setBusy] = useState(false)
  const [err,  setErr]  = useState('')

  const run = useCallback(() => {
    setBusy(true); setErr('')
    api.sqliteHealth()
      .then(d => setData(d))
      .catch(e => setErr(e.message || String(e)))
      .finally(() => setBusy(false))
  }, [])

  useEffect(() => { run() }, [run])

  const verdict = data && (STATUS[data.verdict] || STATUS.warn)

  return (
    <div style={{ display:'flex', flexDirection:'column', gap:16 }}>
      <div className="card">
        <div style={{ display:'flex', justifyContent:'space-between', alignItems:'center', flexWrap:'wrap', gap:10 }}>
          <h3 style={{ fontSize:14, margin:0 }}>Sante base SQLite (master_scan.db)</h3>
          <div style={{ display:'flex', alignItems:'center', gap:10 }}>
            {verdict && (
              <span style={{ fontSize:12, fontWeight:600, padding:'4px 10px', borderRadius:4,
                background:verdict.color + '22', color:verdict.color }}>
                Verdict : {verdict.label}
              </span>
            )}
            <button className="btn-primary" onClick={run} disabled={busy} style={{ fontSize:12, padding:'6px 12px' }}>
              {busy ? '⟳ ...' : '↻ Relancer'}
            </button>
          </div>
        </div>
        <div style={{ marginTop:8, fontSize:11, color:'var(--muted)' }}>
          27 controles en lecture seule stricte (connexion mode=ro) : schema, index, integrite SQLite, contenu, scan_meta, coherence avec le CSV fige.
        </div>
      </div>

      {err && (
        <div className="card" style={{ color:'var(--danger)', fontSize:13 }}>{'✗'} {err}</div>
      )}

      {data && (
        <div className="card" style={{ padding:0, overflow:'hidden' }}>
          {data.checks.map((c, i) => (
            <div key={c.id} style={{ display:'flex', alignItems:'center', gap:12, padding:'10px 14px',
              borderBottom: i < data.checks.length - 1 ? '1px solid var(--border)' : 'none', flexWrap:'wrap' }}>
              <div style={{ minWidth:96 }}><Badge status={c.status} /></div>
              <div style={{ flex:'1 1 200px', minWidth:0 }}>
                <div style={{ fontSize:13, color:'var(--text)' }}>{c.label}</div>
                {c.detail && <div className="mono" style={{ fontSize:11, color:'var(--muted)', wordBreak:'break-word', marginTop:2 }}>{c.detail}</div>}
              </div>
            </div>
          ))}
        </div>
      )}

      {!data && !err && busy && (
        <div className="card" style={{ color:'var(--muted)', fontSize:13 }}>{'⟳'} Diagnostic en cours...</div>
      )}
    </div>
  )
}
