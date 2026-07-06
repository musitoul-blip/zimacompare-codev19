/**
 * SetupWizard.jsx — Wizard de première installation ZimaCompare v18
 *
 * Affiché à la place de l'app normale si status.setup_needed === true.
 * 3 étapes :
 *   1. Configuration rclone.conf (upload ou chemin)
 *   2. Finalisation (mount + restart rclone + vérification pCloud)
 *   3. Succès → redirection automatique
 */
import { useState, useRef } from 'react'

const BASE = '/api/setup'

async function apiSetup(method, path, body) {
  const r = await fetch(BASE + path, {
    method,
    headers: body && !(body instanceof FormData)
      ? { 'Content-Type': 'application/json' }
      : {},
    body: body instanceof FormData
      ? body
      : body ? JSON.stringify(body) : undefined,
  })
  if (!r.ok) {
    const err = await r.json().catch(() => ({ detail: r.statusText }))
    throw new Error(err.detail || r.statusText)
  }
  return r.json()
}

// Couleurs et styles partagés
const styles = {
  container: {
    minHeight: '100vh',
    display: 'flex',
    flexDirection: 'column',
    alignItems: 'center',
    justifyContent: 'center',
    padding: '24px',
    background: 'var(--bg)',
  },
  card: {
    background: 'var(--surface)',
    border: '1px solid var(--border)',
    borderRadius: '12px',
    padding: '40px',
    maxWidth: '560px',
    width: '100%',
  },
  title: {
    fontSize: '22px',
    fontWeight: 700,
    marginBottom: '8px',
    color: 'var(--text)',
  },
  subtitle: {
    fontSize: '14px',
    color: 'var(--muted)',
    marginBottom: '32px',
    lineHeight: 1.6,
  },
  stepIndicator: {
    display: 'flex',
    gap: '8px',
    marginBottom: '32px',
  },
  stepDot: (active, done) => ({
    width: '8px',
    height: '8px',
    borderRadius: '50%',
    background: done ? 'var(--accent)' : active ? 'var(--accent)' : 'var(--border)',
    opacity: done ? 1 : active ? 1 : 0.4,
    transition: 'all 0.3s',
  }),
  label: {
    fontSize: '13px',
    color: 'var(--muted)',
    marginBottom: '8px',
    display: 'block',
  },
  input: {
    width: '100%',
    padding: '10px 12px',
    background: 'var(--bg)',
    border: '1px solid var(--border)',
    borderRadius: '6px',
    color: 'var(--text)',
    fontSize: '13px',
    fontFamily: 'monospace',
    boxSizing: 'border-box',
  },
  btn: (disabled) => ({
    padding: '10px 24px',
    background: disabled ? 'var(--border)' : 'var(--accent)',
    color: disabled ? 'var(--muted)' : '#fff',
    border: 'none',
    borderRadius: '6px',
    fontSize: '14px',
    fontWeight: 600,
    cursor: disabled ? 'not-allowed' : 'pointer',
    transition: 'opacity 0.2s',
  }),
  btnSecondary: {
    padding: '10px 24px',
    background: 'transparent',
    color: 'var(--muted)',
    border: '1px solid var(--border)',
    borderRadius: '6px',
    fontSize: '14px',
    cursor: 'pointer',
  },
  error: {
    background: 'rgba(239,68,68,0.1)',
    border: '1px solid rgba(239,68,68,0.3)',
    borderRadius: '6px',
    padding: '12px',
    color: '#fca5a5',
    fontSize: '13px',
    marginTop: '16px',
  },
  success: {
    background: 'rgba(34,197,94,0.1)',
    border: '1px solid rgba(34,197,94,0.3)',
    borderRadius: '6px',
    padding: '12px',
    color: '#86efac',
    fontSize: '13px',
    marginTop: '16px',
  },
  divider: {
    display: 'flex',
    alignItems: 'center',
    gap: '12px',
    margin: '20px 0',
    color: 'var(--muted)',
    fontSize: '12px',
  },
  dividerLine: {
    flex: 1,
    height: '1px',
    background: 'var(--border)',
  },
  progressItem: (status) => ({
    display: 'flex',
    alignItems: 'center',
    gap: '12px',
    padding: '10px 0',
    borderBottom: '1px solid var(--border)',
    fontSize: '13px',
    color: status === 'error' ? '#fca5a5'
         : status === 'done'  ? '#86efac'
         : status === 'running' ? 'var(--accent)'
         : 'var(--muted)',
  }),
  progressIcon: (status) => ({
    fontSize: '16px',
    minWidth: '20px',
    textAlign: 'center',
  }),
}

const STEP_ICONS = {
  idle:    '○',
  running: '⟳',
  done:    '✓',
  error:   '✗',
}

// ---------------------------------------------------------------------------
// Étape 1 — rclone.conf
// ---------------------------------------------------------------------------
function StepRclone({ onSuccess }) {
  const [mode, setMode]       = useState('upload') // 'upload' | 'path'
  const [path, setPath]       = useState('')
  const [file, setFile]       = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError]     = useState(null)
  const fileRef               = useRef()

  async function handleSubmit() {
    setError(null)
    setLoading(true)
    try {
      if (mode === 'upload') {
        if (!file) throw new Error('Sélectionne un fichier rclone.conf')
        const fd = new FormData()
        fd.append('file', file)
        await apiSetup('POST', '/upload-rclone', fd)
      } else {
        if (!path.trim()) throw new Error('Saisis le chemin du fichier rclone.conf')
        await apiSetup('POST', '/rclone-path', { path: path.trim() })
      }
      onSuccess()
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }

  return (
    <div>
      <p style={styles.subtitle}>
        ZimaCompare a besoin de ton fichier <code>rclone.conf</code> pour accéder à pCloud.
        Ce fichier contient ton jeton d'accès personnel — il ne quitte pas ta Zima.
      </p>

      {/* Sélecteur de mode */}
      <div style={{ display: 'flex', gap: '8px', marginBottom: '20px' }}>
        {[
          { id: 'upload', label: '⬆ Uploader le fichier' },
          { id: 'path',   label: '📂 Chemin sur la Zima' },
        ].map(m => (
          <button
            key={m.id}
            onClick={() => setMode(m.id)}
            style={{
              padding: '8px 16px',
              background: mode === m.id ? 'rgba(79,142,247,0.15)' : 'transparent',
              border: `1px solid ${mode === m.id ? 'var(--accent)' : 'var(--border)'}`,
              borderRadius: '6px',
              color: mode === m.id ? 'var(--accent)' : 'var(--muted)',
              fontSize: '13px',
              cursor: 'pointer',
            }}
          >
            {m.label}
          </button>
        ))}
      </div>

      {mode === 'upload' ? (
        <div>
          <label style={styles.label}>Fichier rclone.conf</label>
          <div
            onClick={() => fileRef.current?.click()}
            style={{
              border: '2px dashed var(--border)',
              borderRadius: '8px',
              padding: '24px',
              textAlign: 'center',
              cursor: 'pointer',
              color: file ? 'var(--accent)' : 'var(--muted)',
              fontSize: '13px',
              transition: 'border-color 0.2s',
            }}
          >
            {file ? `✓ ${file.name}` : 'Cliquer pour sélectionner rclone.conf'}
          </div>
          <input
            ref={fileRef}
            type="file"
            accept=".conf"
            style={{ display: 'none' }}
            onChange={e => setFile(e.target.files[0])}
          />
          <p style={{ fontSize: '12px', color: 'var(--muted)', marginTop: '8px' }}>
            Le fichier est accessible via SMB sur ta Zima dans{' '}
            <code>/DATA/AppData/zimacompare-v18/rclone/rclone.conf</code>
          </p>
        </div>
      ) : (
        <div>
          <label style={styles.label}>Chemin complet du fichier sur la Zima</label>
          <input
            style={styles.input}
            value={path}
            onChange={e => setPath(e.target.value)}
            placeholder="/DATA/AppData/zimacompare-v18/rclone/rclone.conf"
          />
          <p style={{ fontSize: '12px', color: 'var(--muted)', marginTop: '8px' }}>
            Le chemin doit être accessible depuis le conteneur.
          </p>
        </div>
      )}

      {error && <div style={styles.error}>⚠ {error}</div>}

      <div style={{ marginTop: '24px', display: 'flex', justifyContent: 'flex-end' }}>
        <button
          style={styles.btn(loading || (mode === 'upload' && !file))}
          disabled={loading || (mode === 'upload' && !file)}
          onClick={handleSubmit}
        >
          {loading ? '⟳ Validation...' : 'Valider →'}
        </button>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Étape 2 — Finalisation
// ---------------------------------------------------------------------------
// v18 (AIO) : les étapes correspondent aux identifiants émis par
// /api/setup/finalize/stream — plus de 'mountpoint' ni 'rclone_restart'
// (S6 monte rclone tout seul dès que rclone.conf est déposé).
const FINALIZE_STEPS = [
  { id: 'rclone_conf', label: 'Vérification de rclone.conf' },
  { id: 'pcloud_wait', label: 'Montage et connexion à pCloud' },
  { id: 'done',        label: 'Finalisation' },
]

function StepFinalize({ onSuccess }) {
  const [steps, setSteps]     = useState({})
  const [running, setRunning] = useState(false)
  const [error, setError]     = useState(null)

  function updateStep(id, status, message = '') {
    setSteps(prev => ({ ...prev, [id]: { status, message } }))
  }

  async function handleFinalize() {
    setRunning(true)
    setError(null)
    setSteps({})

    try {
      const evtSource = new EventSource('/api/setup/finalize/stream')

      evtSource.onmessage = (e) => {
        const data = JSON.parse(e.data)
        updateStep(data.step, data.status, data.message)

        if (data.step === 'done' && data.status === 'done') {
          evtSource.close()
          setTimeout(onSuccess, 1000)
        }
        if (data.status === 'error') {
          evtSource.close()
          setError(data.message)
          setRunning(false)
        }
      }

      evtSource.onerror = () => {
        evtSource.close()
        setError('Connexion au serveur interrompue')
        setRunning(false)
      }
    } catch (e) {
      setError(e.message)
      setRunning(false)
    }
  }

  const allDone = steps['done']?.status === 'done'

  return (
    <div>
      <p style={styles.subtitle}>
        ZimaCompare va monter pCloud et démarrer tous les services.
        Le montage peut prendre jusqu'à une minute.
      </p>

      {/* Liste des étapes */}
      <div style={{ marginBottom: '24px' }}>
        {FINALIZE_STEPS.map(step => {
          const s = steps[step.id]
          const status = s?.status || 'idle'
          return (
            <div key={step.id} style={styles.progressItem(status)}>
              <span style={styles.progressIcon(status)}>
                {STEP_ICONS[status] || '○'}
              </span>
              <span style={{ flex: 1 }}>{step.label}</span>
              {s?.message && status !== 'done' && (
                <span style={{ fontSize: '11px', opacity: 0.7 }}>{s.message}</span>
              )}
            </div>
          )
        })}
      </div>

      {error && <div style={styles.error}>⚠ {error}</div>}
      {allDone && <div style={styles.success}>✓ Installation terminée — pCloud connecté !</div>}

      {!running && !allDone && (
        <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
          <button style={styles.btn(false)} onClick={handleFinalize}>
            Lancer l'installation →
          </button>
        </div>
      )}

      {running && !allDone && (
        <div style={{ textAlign: 'center', color: 'var(--muted)', fontSize: '13px' }}>
          ⟳ Installation en cours...
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Composant principal SetupWizard
// ---------------------------------------------------------------------------
export default function SetupWizard({ onComplete }) {
  const [step, setStep] = useState(1) // 1 = rclone, 2 = finalize, 3 = done

  function handleRcloneSuccess() {
    setStep(2)
  }

  function handleFinalizeSuccess() {
    setStep(3)
    setTimeout(onComplete, 2000)
  }

  return (
    <div style={styles.container}>
      <div style={styles.card}>
        {/* Logo + titre */}
        <div style={{ display: 'flex', alignItems: 'center', gap: '12px', marginBottom: '24px' }}>
          <span style={{ fontSize: '28px' }}>🔄</span>
          <div>
            <div style={{ fontWeight: 700, fontSize: '18px', color: 'var(--text)' }}>
              ZimaCompare v18
            </div>
            <div style={{ fontSize: '12px', color: 'var(--muted)' }}>
              Configuration initiale
            </div>
          </div>
        </div>

        {/* Indicateur d'étapes */}
        <div style={styles.stepIndicator}>
          {[1, 2, 3].map(s => (
            <div key={s} style={styles.stepDot(s === step, s < step)} />
          ))}
          <span style={{ fontSize: '12px', color: 'var(--muted)', marginLeft: '8px' }}>
            Étape {Math.min(step, 3)} / 3
          </span>
        </div>

        {/* Titre de l'étape */}
        <div style={styles.title}>
          {step === 1 && '① Configurer pCloud'}
          {step === 2 && '② Connexion pCloud'}
          {step === 3 && '✓ Prêt !'}
        </div>

        {/* Contenu */}
        {step === 1 && <StepRclone onSuccess={handleRcloneSuccess} />}
        {step === 2 && <StepFinalize onSuccess={handleFinalizeSuccess} />}
        {step === 3 && (
          <div style={{ textAlign: 'center', padding: '20px 0' }}>
            <div style={{ fontSize: '48px', marginBottom: '16px' }}>🎉</div>
            <p style={{ color: 'var(--muted)', fontSize: '14px' }}>
              ZimaCompare est prêt. Redirection en cours...
            </p>
          </div>
        )}
      </div>

      {/* Note de sécurité */}
      <p style={{
        marginTop: '16px',
        fontSize: '11px',
        color: 'var(--muted)',
        textAlign: 'center',
        maxWidth: '400px',
      }}>
        🔒 Le fichier rclone.conf reste sur ta Zima et n'est jamais transmis à l'extérieur.
      </p>
    </div>
  )
}
