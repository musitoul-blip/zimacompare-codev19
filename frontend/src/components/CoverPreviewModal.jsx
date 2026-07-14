import { useState, useEffect } from 'react'
import { api, fmtSize } from '../api.js'

const muted = { color: 'var(--muted)', fontSize: 13 }

// ============================================================================
// Modale d'apercu avant/apres + correction pochette (LOT 5c/5d/5e/5g).
// Extraite de TabBluos.jsx au LOT 8b pour etre partagee avec TabCovers (LOT 8c).
// Composant purement presentationnel : la logique (reglages par album, clef
// d'etat, appel /apply, polling de progression) reste chez l'appelant, qui
// fournit `settings` deja fusionnes et `busy`/`message` deja resolus pour
// l'album affiche.
// ============================================================================

export default function CoverPreviewModal({ album, settings, onSettingChange, onReset, busy, message, onApply, onClose }) {
  const [afterInfo, setAfterInfo] = useState(null)
  const [afterInfoLoading, setAfterInfoLoading] = useState(false)
  const [afterInfoErr, setAfterInfoErr] = useState('')

  useEffect(() => {
    let alive = true
    setAfterInfoLoading(true); setAfterInfoErr('')
    api.coverPreviewInfo({ path: album.sample_path, maxKb: settings.maxKb, maxPx: settings.maxPx, allowDownscale: settings.allowDownscale })
      .then(info => { if (alive) setAfterInfo(info) })
      .catch(e => { if (alive) setAfterInfoErr(e.message) })
      .finally(() => { if (alive) setAfterInfoLoading(false) })
    return () => { alive = false }
  }, [album, settings])

  const common = { path: album.sample_path, maxKb: settings.maxKb, maxPx: settings.maxPx, allowDownscale: settings.allowDownscale }

  return (
    <div style={{
      position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.5)',
      display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 1000,
    }} onClick={onClose}>
      <div style={{
        background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 8,
        padding: 20, width: '95vw', height: '90vh', overflow: 'hidden',
        display: 'flex', flexDirection: 'column',
      }} onClick={e => e.stopPropagation()}>
        <h3 style={{ marginTop: 0, marginBottom: 8, fontSize: 15, flex: '0 0 auto' }}>
          {album.artist} — {album.title}
        </h3>

        <div style={{ display: 'flex', gap: 12, alignItems: 'center', marginBottom: 12, flexWrap: 'wrap', flex: '0 0 auto' }}>
          <label style={{ fontSize: 13 }}>Poids max (Ko)
            <input type="number" value={settings.maxKb}
                   onChange={e => onSettingChange({ maxKb: Number(e.target.value) })}
                   style={{ width: 80, marginLeft: 4 }} />
          </label>
          <label style={{ fontSize: 13 }}>Dimension max (px)
            <input type="number" value={settings.maxPx}
                   onChange={e => onSettingChange({ maxPx: Number(e.target.value) })}
                   style={{ width: 80, marginLeft: 4 }} />
          </label>
          <label style={{ fontSize: 13 }}>
            <input type="checkbox" checked={settings.allowDownscale}
                   onChange={e => onSettingChange({ allowDownscale: e.target.checked })} />
            {' '}Réduire davantage si le poids max n'est pas atteint
          </label>
          <span style={{ fontSize: 11, color: 'var(--muted)' }}>
            (la dimension max s'applique toujours — borne dure. Cette case autorise une réduction
            supplémentaire des dimensions, par paliers de 15%, jusqu'à 300px minimum, si le poids
            reste au-dessus du maximum après compression JPEG)
          </span>
          <button onClick={onReset} style={{ fontSize: 12 }}>
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
              {album.cover_format || '?'} · {album.cover_width}×{album.cover_height} · {fmtSize(album.cover_size)}
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
          {message && <span style={{ fontSize: 12 }}>{message}</span>}
          <button className="btn-primary" disabled={busy} onClick={onApply}>
            {busy ? '...' : 'Corriger avec ces réglages'}
          </button>
          <button onClick={onClose}>Fermer</button>
        </div>
      </div>
    </div>
  )
}
