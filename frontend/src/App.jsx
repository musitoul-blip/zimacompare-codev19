import { useState, useEffect, useCallback } from 'react'
import { api } from './api.js'
import SetupWizard from './components/SetupWizard.jsx'
import TabScanSync from './components/TabScanSync.jsx'
import TabHistory  from './components/TabHistory.jsx'
import TabCleanup  from './components/TabCleanup.jsx'
import TabCloud    from './components/TabCloud.jsx'
import TabSystem   from './components/TabSystem.jsx'
import TabInfo     from './components/TabInfo.jsx'
import TabZimatag from './components/TabZimatag.jsx'
import TabVerification from './components/TabVerification.jsx'
import TabAuditBdd from './components/TabAuditBdd.jsx'
import TabAuditRegistry from './components/TabAuditRegistry.jsx'  // T10 Lot F4
import TabLogs from './components/TabLogs.jsx'  // A2
import TabTests from './components/TabTests.jsx'  // A3
import TabBluos from './components/TabBluos.jsx'  // Lot 5 BluOS
import TabCovers from './components/TabCovers.jsx'  // LOT 8c

document.title = 'ZimaCompare&Tag v' + __APP_VERSION__
const STATE_COLOR = {
  IDLE:      '#64748b',
  SCANNING:  '#4f8ef7',
  COMPARING: '#7c3aed',
  SYNCING:   '#22c55e',
  VERIFYING: '#06b6d4',
  ERROR:     '#ef4444',
}

export default function App() {
  const [tab,        setTab]        = useState('scan')
  const [status,     setStatus]     = useState(null)
  const [error,      setError]      = useState(null)
  const [setupDone,  setSetupDone]  = useState(false)

  const poll = useCallback(async () => {
    try {
      const s = await api.status()
      setStatus(s)
      setError(null)
      // Si le setup était nécessaire et est maintenant terminé
      if (!s.setup_needed) {
        setSetupDone(true)
      }
    } catch (e) {
      setError('Backend inaccessible')
    }
  }, [])

  useEffect(() => {
    poll()
    const id = setInterval(poll, 2000)
    return () => clearInterval(id)
  }, [poll])

  // Afficher le wizard si setup_needed et non encore complété
  const needsSetup = status?.setup_needed === true && !setupDone

  if (needsSetup) {
    return (
      <SetupWizard onComplete={() => {
        setSetupDone(true)
        poll() // Rafraîchir le statut
      }} />
    )
  }

  const isActive = status?.app_state && !['IDLE','ERROR'].includes(status.app_state)

  return (
    <div style={{ display:'flex', flexDirection:'column', minHeight:'100vh' }}>
      <div style={{ position:'sticky', top:0, zIndex:50 }}>
      <header style={{
        background: 'var(--surface)', borderBottom: '1px solid var(--border)',
        padding: '0 24px', display:'flex', alignItems:'center', gap:16, height:52
      }}>
        <img src="/icon.png" alt="" style={{ height:24, width:24, borderRadius:5 }} />
        <span style={{ fontWeight:700, fontSize:16, letterSpacing:'.02em' }}>ZimaCompare&Tag</span>
        <span style={{ color:'var(--muted)', fontSize:12 }}>v{__APP_VERSION__}</span>
        <div style={{ flex:1 }} />
        {error ? (
          <span className="badge badge-red">⚠ {error}</span>
        ) : status ? (
          <span className="badge" style={{
            background: (STATE_COLOR[status.app_state] || '#64748b') + '22',
            color: STATE_COLOR[status.app_state] || '#64748b',
          }}>
            {isActive && '⟳ '}{status.app_state}
          </span>
        ) : null}
      </header>

      {status?.scan_meta && status.scan_meta.last_scan_completed && (status.scan_meta.last_scan_status !== 'completed' || status.scan_meta.last_scan_partial) && (
        <div style={{
          padding:'8px 24px', background:'color-mix(in srgb, var(--warning) 16%, transparent)',
          borderBottom:'1px solid var(--border)', fontSize:13,
          display:'flex', alignItems:'center', justifyContent:'space-between', gap:16,
        }}>
          <span>
            ⚠ {status.scan_meta.last_scan_status !== 'completed'
              ? `Base incomplète — le dernier scan ne s'est pas terminé (statut : ${status.scan_meta.last_scan_status}). Les audits et exports portent sur une partie de la bibliothèque. Un scan complet est nécessaire.`
              : `Base partielle — dernier scan limité à ${status.scan_meta.last_scan_scope}. Les audits et exports ne couvrent pas toute la bibliothèque.`}
          </span>
          <span style={{ fontSize:11, color:'var(--muted)', whiteSpace:'nowrap' }}>
            {status.scan_meta.last_scan_count} pistes · {(status.scan_meta.last_scan_completed || '').slice(0,16).replace('T',' ')}
          </span>
        </div>
      )}

      <nav style={{
        background:'var(--surface)', borderBottom:'1px solid var(--border)',
        display:'flex', padding:'0 24px', flexWrap:'wrap',
      }}>
        {[
          { id:'scan',    label:'🔍 Scan & Sync' },
          { id:'zimatag', label:'🏷 ZimaTAG' },
          { id:'bluos',   label:'📻 BluOS' },
          { id:'covers',  label:'🖼 Pochettes' },
          { id:'auditreg', label:'🛠 Audit ZimaTAG' },
          { id:'cleanup', label:'🧹 Nettoyage .db' },
          { id:'cloud',   label:'☁ Cloud'        },
          { id:'history', label:'📋 Historique'  },
          { id:'system',  label:'⚙ Système'     },
          { id:'info',    label:'📊 Information' },
          { id:'logs',    label:'📜 Logs' },
          { id:'tests',   label:'🧪 Tests' },
          { id:'verif',   label:'✅ Verification' },
          { id:'auditbdd', label:'🗄️ Audit base de données' },
        ].map(t => (
          <button key={t.id} onClick={() => setTab(t.id)} style={{
            background:'none', border:'none', color: tab===t.id ? 'var(--accent)' : 'var(--muted)',
            borderBottom: tab===t.id ? '2px solid var(--accent)' : '2px solid transparent',
            padding:'12px 16px', borderRadius:0, fontSize:13, fontWeight: tab===t.id ? 600 : 400,
          }}>
            {t.label}
          </button>
        ))}
      </nav>
      </div>

      <main style={{ flex:1, padding:24, maxWidth: tab === 'auditreg' ? 1500 : 1100, width:'100%', margin:'0 auto' }}>  {/* T10 Lot H4 : registre plus large */}
        {tab === 'scan'    && <TabScanSync status={status} />}
        {tab === 'zimatag' && <TabZimatag status={status} />}
        {tab === 'bluos'   && <TabBluos status={status} />}
        {tab === 'covers'  && <TabCovers />}
        {tab === 'auditreg' && <TabAuditRegistry />}
        {tab === 'cleanup' && <TabCleanup  status={status} />}
        {tab === 'cloud'   && <TabCloud    status={status} />}
        {tab === 'history' && <TabHistory />}
        {tab === 'system'  && <TabSystem />}
        {tab === 'info'    && <TabInfo />}
        {tab === 'logs'    && <TabLogs />}
        {tab === 'tests'   && <TabTests />}
        {tab === 'verif'   && <TabVerification />}
        {tab === 'auditbdd' && <TabAuditBdd />}
      </main>
    </div>
  )
}
