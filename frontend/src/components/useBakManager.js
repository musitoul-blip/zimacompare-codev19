import { useState } from 'react'
import { api } from '../api.js'

// ============================================================================
// Etat + logique de gestion des .bak (LOT 5b), mutualises entre TabBluos et
// TabCovers (LOT 8d) pour eviter la duplication qu'on a deja evitee cote JSX
// via BakManagerModal. Chaque composant appelant obtient sa propre instance
// (pas de partage d'etat entre onglets) -- identique au comportement actuel
// ou chaque onglet gere son propre cycle ouverture/fermeture de la modale.
// LOT 8e : ajout delete-redundant (suppression source des .bak deja
// archives) et purge-archive (vidage irreversible de l'archive).
// ============================================================================

export function useBakManager() {
  const [baksOpen, setBaksOpen] = useState(false)
  const [baksLoading, setBaksLoading] = useState(false)
  const [baksErr, setBaksErr] = useState('')
  const [baksList, setBaksList] = useState(null)
  const [baksMoving, setBaksMoving] = useState(false)
  const [baksReport, setBaksReport] = useState(null)
  const [baksDeleting, setBaksDeleting] = useState(false)
  const [baksDeleteReport, setBaksDeleteReport] = useState(null)
  const [baksPurging, setBaksPurging] = useState(false)
  const [baksPurgeReport, setBaksPurgeReport] = useState(null)

  async function openBaks() {
    setBaksOpen(true)
    setBaksReport(null); setBaksDeleteReport(null); setBaksPurgeReport(null)
    setBaksErr(''); setBaksList(null); setBaksLoading(true)
    try { setBaksList(await api.coverBaks()) }
    catch (e) { setBaksErr(e.message) }
    finally { setBaksLoading(false) }
  }
  function closeBaks() {
    setBaksOpen(false); setBaksList(null)
    setBaksReport(null); setBaksDeleteReport(null); setBaksPurgeReport(null)
    setBaksErr('')
  }
  async function moveBaks() {
    setBaksMoving(true); setBaksErr('')
    try { setBaksReport(await api.coverBaksMove()) }
    catch (e) {
      setBaksErr(e.status === 403 ? '⏳ Écriture désactivée (COVER_ALLOW_WRITE=false)' : '✗ ' + e.message)
    } finally { setBaksMoving(false) }
  }
  async function deleteRedundantBaks() {
    setBaksDeleting(true); setBaksErr('')
    try {
      setBaksDeleteReport(await api.coverBaksDeleteRedundant())
      setBaksList(await api.coverBaks())
    } catch (e) {
      setBaksErr(e.status === 403 ? '⏳ Écriture désactivée (COVER_ALLOW_WRITE=false)' : '✗ ' + e.message)
    } finally { setBaksDeleting(false) }
  }
  async function purgeArchive() {
    setBaksPurging(true); setBaksErr('')
    try {
      setBaksPurgeReport(await api.coverBaksPurgeArchive())
      setBaksList(await api.coverBaks())
    } catch (e) {
      setBaksErr(e.status === 403 ? '⏳ Écriture désactivée (COVER_ALLOW_WRITE=false)' : '✗ ' + e.message)
    } finally { setBaksPurging(false) }
  }

  return {
    baksOpen, baksLoading, baksErr, baksList,
    baksMoving, baksReport, openBaks, closeBaks, moveBaks,
    baksDeleting, baksDeleteReport, deleteRedundantBaks,
    baksPurging, baksPurgeReport, purgeArchive,
  }
}
