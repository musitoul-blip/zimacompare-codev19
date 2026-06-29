import { useState, useEffect, useRef, useCallback } from 'react'
import { openWsLogs } from '../api.js'

const LVL = {
  ERROR: '#ef4444', WARNING: '#f59e0b', WARN: '#f59e0b',
  INFO: '#22c55e', DEBUG: '#94a3b8',
}
const HS = { ok:'#22c55e', warn:'#f59e0b', crit:'#ef4444', unknown:'#94a3b8' }

function lineLevel(line) {
  const m = /^\d{2}:\d{2}:\d{2}\s+\[([A-Z]+)/.exec(line)
  return m ? m[1] : ''
}

export default function TabLogs() {
  const [lines, setLines] = useState([])
  const [paused, setPaused] = useState(false)
  const [q, setQ] = useState('')
  const [minLvl, setMinLvl] = useState('ALL')
  const [onlyFront, setOnlyFront] = useState(false)
  const [health, setHealth] = useState(null)
  const boxRef = useRef(null)
  const pausedRef = useRef(false)
  const bufRef = useRef([])

  useEffect(() => { pausedRef.current = paused }, [paused])

  useEffect(() => {
    const ws = openWsLogs((line) => {
      bufRef.current.push(line)
      if (bufRef.current.length > 2000) bufRef.current = bufRef.current.slice(-2000)
      if (!pausedRef.current) setLines(bufRef.current.slice())
    })
    return () => { try { ws.close() } catch (e) { /* noop */ } }
  }, [])

  useEffect(() => {
    if (paused) return
    const el = boxRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [lines, paused])

  const loadHealth = useCallback(() => {
    fetch('/api/diag/health').then(r => r.json()).then(setHealth).catch(() => {})
  }, [])
  useEffect(() => { loadHealth() }, [loadHealth])

  const order = { DEBUG:0, INFO:1, WARNING:2, WARN:2, ERROR:3 }
  const minRank = minLvl === 'ALL' ? -1 : (order[minLvl] ?? -1)
  const view = lines.filter(l => {
    if (onlyFront && l.indexOf('[FRONT]') < 0) return false
    if (minRank >= 0) {
      const lv = lineLevel(l)
      if ((order[lv] ?? -1) < minRank) return false
    }
    if (q && l.toLowerCase().indexOf(q.toLowerCase()) < 0) return false
    return true
  })

  const dl = (fmt) => {
    const a = document.createElement('a')
    a.href = '/api/diag/report?format=' + fmt
    a.download = ''
    document.body.appendChild(a); a.click(); a.remove()
  }

  return (
    <div style={{ display:'flex', flexDirection:'column', gap:12 }}>
      <div className="card">
        <div style={{ display:'flex', gap:10, alignItems:'center', flexWrap:'wrap' }}>
          <strong>Logs en direct</strong>
          <button onClick={() => setPaused(p => !p)}>{paused ? '▶ Reprendre' : '⏸ Pause'}</button>
          <button onClick={() => { bufRef.current = []; setLines([]) }}>✕ Vider</button>
          <select value={minLvl} onChange={e => setMinLvl(e.target.value)}>
            <option value="ALL">Tous niveaux</option>
            <option value="INFO">INFO+</option>
            <option value="WARNING">WARNING+</option>
            <option value="ERROR">ERROR seul</option>
          </select>
          <label style={{ fontSize:13 }}>
            <input type="checkbox" checked={onlyFront} onChange={e => setOnlyFront(e.target.checked)} /> [FRONT] seul
          </label>
          <input placeholder="filtrer..." value={q} onChange={e => setQ(e.target.value)}
            style={{ flex:1, minWidth:120 }} />
          <span style={{ fontSize:12, color:'#94a3b8' }}>{view.length}/{lines.length}</span>
        </div>
        <div style={{ marginTop:10, display:'flex', gap:10, flexWrap:'wrap' }}>
          <button onClick={() => dl('md')}>⬇ Rapport diagnostic (.md)</button>
          <button onClick={() => dl('json')}>⬇ Rapport (.json)</button>
          <button onClick={loadHealth}>↻ Rafraichir sante</button>
          {health && (
            <span style={{ fontSize:13 }}>
              Sante : <b style={{ color: HS[health.verdict] || HS.unknown }}>{health.verdict}</b>
              {health.metrics && (
                <span style={{ color:'#94a3b8' }}>
                  {' '}· CPU {health.metrics.cpu_percent}% · RAM {health.metrics.mem_percent}% · load/cpu {health.metrics.load_per_cpu}
                </span>
              )}
            </span>
          )}
        </div>
      </div>
      <div ref={boxRef} style={{
        fontFamily:'monospace', fontSize:12, lineHeight:1.5, background:'#0b1220', color:'#cbd5e1',
        padding:12, borderRadius:8, height:'60vh', overflow:'auto', whiteSpace:'pre-wrap', wordBreak:'break-word'
      }}>
        {view.map((l, i) => {
          const lv = lineLevel(l)
          const col = LVL[lv] || '#cbd5e1'
          const isFront = l.indexOf('[FRONT]') >= 0
          return (
            <div key={i} style={{ color: col, background: isFront ? '#1e293b55' : 'transparent' }}>{l}</div>
          )
        })}
        {view.length === 0 && <div style={{ color:'#64748b' }}>(aucune ligne)</div>}
      </div>
    </div>
  )
}
