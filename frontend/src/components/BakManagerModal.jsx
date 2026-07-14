import { fmtSize } from '../api.js'

const muted = { color: 'var(--muted)', fontSize: 13 }

// ============================================================================
// Modale de gestion des .bak (LOT 5b). Extraite de TabBluos.jsx au LOT 8d
// pour etre partagee avec TabCovers. Composant purement presentationnel :
// l'appelant garde l'etat (liste/chargement/rapport) et les appels API
// (coverBaks/coverBaksMove/...), portes par le hook useBakManager -- meme
// principe que CoverPreviewModal (LOT 8b).
// LOT 8e : deux actions destructives supplementaires -- suppression des .bak
// redondants (deja archives) et purge irreversible de l'archive, chacune
// confirmee cote front avant l'appel (double confirmation pour la purge).
// ============================================================================

export default function BakManagerModal({
  loading, err, list,
  moving, report, onMove,
  deleting, deleteReport, onDeleteRedundant,
  purging, purgeReport, onPurgeArchive,
  onClose,
}) {
  const anyReport = report || deleteReport || purgeReport
  const busy = moving || deleting || purging

  function handleDeleteRedundant() {
    if (!confirm(`Supprimer ${list.already_archived_count} .bak de la source ? Leurs sauvegardes sont déjà archivées dans 00_A_supp.`)) return
    onDeleteRedundant()
  }
  function handlePurge() {
    const first = `ATTENTION : cette action est IRRÉVERSIBLE. Les ${list.archive_count} sauvegardes originales seront définitivement supprimées. Aucune restauration ne sera possible après cette opération. Confirmez-vous ?`
    if (!confirm(first)) return
    const second = `Dernière confirmation : vider définitivement l'archive 00_A_supp (${list.archive_count} fichiers, ${fmtSize(list.archive_size)}) ? Cette action ne peut pas être annulée.`
    if (!confirm(second)) return
    onPurgeArchive()
  }

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

        {!anyReport && list && (
          <>
            <div style={{ ...muted, marginBottom: 4 }}>
              {list.total} fichier(s) en source, {fmtSize(list.total_size)} — {list.root}
            </div>
            <div style={{ ...muted, marginBottom: 8, fontSize: 12 }}>
              {list.already_archived_count} déjà archivé(s) · archive : {list.archive_count} fichier(s), {fmtSize(list.archive_size)}
            </div>
            <div style={{ overflowY: 'auto', flex: 1, border: '1px solid var(--border)', borderRadius: 4 }}>
              {list.files.length === 0 && <div style={{ ...muted, padding: 8 }}>Aucun .bak.</div>}
              {list.files.map((f, i) => (
                <div key={i} style={{ display: 'flex', justifyContent: 'space-between', gap: 8, padding: '4px 8px', borderTop: i ? '1px solid var(--border)' : 'none', fontSize: 12 }}>
                  <span>{f.album} / {f.path.split('/').pop()}{f.already_archived ? ' · déjà archivé' : ''}</span>
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
        {deleteReport && (
          <div style={{ fontSize: 13 }}>
            <div style={{ color: 'var(--success)' }}>{deleteReport.deleted} supprimé(s) (redondants)</div>
            {deleteReport.skipped > 0 && <div style={{ color: 'var(--warning)' }}>{deleteReport.skipped} ignoré(s) (pas de jumeau archivé)</div>}
            {deleteReport.errors > 0 && <div style={{ color: 'var(--danger)' }}>{deleteReport.errors} erreur(s)</div>}
          </div>
        )}
        {purgeReport && (
          <div style={{ fontSize: 13 }}>
            <div style={{ color: 'var(--success)' }}>{purgeReport.deleted} fichier(s) définitivement supprimé(s) de l'archive</div>
            {purgeReport.errors > 0 && <div style={{ color: 'var(--danger)' }}>{purgeReport.errors} erreur(s)</div>}
          </div>
        )}

        <div style={{ display: 'flex', gap: 8, marginTop: 12, justifyContent: 'flex-end', flexWrap: 'wrap' }}>
          {!anyReport && list && (
            <>
              <button className="btn-primary" onClick={onMove} disabled={busy || loading || list.total === 0}>
                {moving ? 'Déplacement...' : `📦 Déplacer vers 00_A_supp (${list.total})`}
              </button>
              {list.already_archived_count > 0 && (
                <button onClick={handleDeleteRedundant} disabled={busy || loading}>
                  {deleting ? 'Suppression...' : `🗑 Supprimer les redondants (${list.already_archived_count})`}
                </button>
              )}
              {list.archive_count > 0 && (
                <button onClick={handlePurge} disabled={busy || loading} style={{ color: 'var(--danger)' }}>
                  {purging ? 'Purge...' : `⚠️ Vider l'archive 00_A_supp (${list.archive_count} fichiers, ${fmtSize(list.archive_size)})`}
                </button>
              )}
            </>
          )}
          <button onClick={onClose}>{anyReport ? 'Fermer' : 'Annuler'}</button>
        </div>
      </div>
    </div>
  )
}
