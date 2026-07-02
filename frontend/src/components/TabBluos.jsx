import { useState, useEffect, useRef } from 'react'
import { api } from '../api.js'

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
  const pollRef = useRef(null)

  // param IP courant (extrait des params pour le champ dedie)
  const ipParam = params.find(p => p.param_key === 'bluos_ip')

  async function load() {
    setLoading(true); setErr('')
    try { setParams(await api.bluosParams()) }
    catch (e) { setErr(e.message) } finally { setLoading(false) }
  }
  useEffect(() => { load() }, [])

  // cleanup polling au demontage
  useEffect(() => () => { if (pollRef.current) clearInterval(pollRef.current) }, [])

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

  const flagged = results ? results.network.filter(r => r.status !== 'ok') : []

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
          <div style={{ ...muted, marginBottom: 8 }}>{flagged.length} album(s) sur {results.network.length} analyse(s)</div>
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

      {/* --- Diagnostic dossiers (volet B) --- */}
      {results && results.folders && results.folders.length > 0 && (
        <div style={card}>
          <h3 style={{ fontSize: 15, marginTop: 0 }}>Diagnostic des dossiers ({results.folders.length})</h3>
          {results.folders.map((f, i) => (
            <div key={i} style={{ borderTop: i ? '1px solid var(--border)' : 'none', padding: '6px 0' }}>
              <div style={{ fontSize: 13, fontWeight: 600 }}>{f.folder}
                {f.matched_network_album && <span style={muted}> — {f.matched_network_album}</span>}
              </div>
              {f.issues && f.issues.map((it, j) => <div key={'i' + j} style={{ fontSize: 12, color: 'var(--danger)' }}>• {it}</div>)}
              {f.notes && f.notes.map((nt, j) => <div key={'n' + j} style={{ fontSize: 12, color: 'var(--warning)' }}>◦ {nt}</div>)}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
