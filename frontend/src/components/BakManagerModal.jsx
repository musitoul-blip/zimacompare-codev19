import { fmtSize } from '../api.js'

const muted = { color: 'var(--muted)', fontSize: 13 }

// ============================================================================
// Modale de gestion des .bak (LOT 5b). Extraite de TabBluos.jsx au LOT 8d
// pour etre partagee avec TabCovers. Composant purement presentationnel :
// l'appelant garde l'etat (liste/chargement/rapport) et les appels API
// (coverBaks/coverBaksMove), portes par le hook useBakManager -- meme
// principe que CoverPreviewModal (LOT 8b).
// ============================================================================

export default function BakManagerModal({ loading, err, list, moving, report, onMove, onClose }) {
  return (
    <div style={{
      position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.5)',
      display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 1000,
    }} onClick={onClose}>
      <div style={{
        background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 8,
        padding: 20, width: 560, maxWidth: '90vw', maxHeight: '80vh',
        display: 'flex', flexDirection: 'column',
      }} onClick={e => e.stopPropagation()}>
        <h3 style={{ marginTop: 0, fontSize: 15 }}>Fichiers .bak</h3>
        {loading && <div style={muted}>Chargement...</div>}
        {err && <div style={{ color: 'var(--danger)', fontSize: 13, marginBottom: 8 }}>✗ {err}</div>}

        {!report && list && (
          <>
            <div style={{ ...muted, marginBottom: 8 }}>
              {list.total} fichier(s), {fmtSize(list.total_size)} — {list.root}
            </div>
            <div style={{ overflowY: 'auto', flex: 1, border: '1px solid var(--border)', borderRadius: 4 }}>
              {list.files.length === 0 && <div style={{ ...muted, padding: 8 }}>Aucun .bak.</div>}
              {list.files.map((f, i) => (
                <div key={i} style={{ display: 'flex', justifyContent: 'space-between', gap: 8, padding: '4px 8px', borderTop: i ? '1px solid var(--border)' : 'none', fontSize: 12 }}>
                  <span>{f.album} / {f.path.split('/').pop()}</span>
                  <span style={muted}>{fmtSize(f.size)}</span>
                </div>
              ))}
            </div>
          </>
        )}

        {report && (
          <div style={{ fontSize: 13 }}>
            <div style={{ color: 'var(--success)' }}>{report.moved} déplacé(s)</div>
            {report.skipped > 0 && <div style={{ color: 'var(--warning)' }}>{report.skipped} ignoré(s) (déjà archivés)</div>}
            {report.errors > 0 && <div style={{ color: 'var(--danger)' }}>{report.errors} erreur(s)</div>}
          </div>
        )}

        <div style={{ display: 'flex', gap: 8, marginTop: 12, justifyContent: 'flex-end' }}>
          {!report && (
            <button className="btn-primary" onClick={onMove}
                    disabled={moving || loading || !list || list.total === 0}>
              {moving ? 'Déplacement...' : 'Déplacer vers 00_A_supp'}
            </button>
          )}
          <button onClick={onClose}>{report ? 'Fermer' : 'Annuler'}</button>
        </div>
      </div>
    </div>
  )
}
