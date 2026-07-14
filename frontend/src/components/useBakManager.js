import { useState } from 'react'
import { api } from '../api.js'

// ============================================================================
// Etat + logique de gestion des .bak (LOT 5b), mutualises entre TabBluos et
// TabCovers (LOT 8d) pour eviter la duplication qu'on a deja evitee cote JSX
// via BakManagerModal. Chaque composant appelant obtient sa propre instance
// (pas de partage d'etat entre onglets) -- identique au comportement actuel
// ou chaque onglet gere son propre cycle ouverture/fermeture de la modale.
// ============================================================================

export function useBakManager() {
  const [baksOpen, setBaksOpen] = useState(false)
  const [baksLoading, setBaksLoading] = useState(false)
  const [baksErr, setBaksErr] = useState('')
  const [baksList, setBaksList] = useState(null)
  const [baksMoving, setBaksMoving] = useState(false)
  const [baksReport, setBaksReport] = useState(null)

  async function openBaks() {
    setBaksOpen(true); setBaksReport(null); setBaksErr(''); setBaksList(null); setBaksLoading(true)
    try { setBaksList(await api.coverBaks()) }
    catch (e) { setBaksErr(e.message) }
    finally { setBaksLoading(false) }
  }
  function closeBaks() {
    setBaksOpen(false); setBaksList(null); setBaksReport(null); setBaksErr('')
  }
  async function moveBaks() {
    setBaksMoving(true); setBaksErr('')
    try { setBaksReport(await api.coverBaksMove()) }
    catch (e) {
      setBaksErr(e.status === 403 ? '⏳ Écriture désactivée (COVER_ALLOW_WRITE=false)' : '✗ ' + e.message)
    } finally { setBaksMoving(false) }
  }

  return { baksOpen, baksLoading, baksErr, baksList, baksMoving, baksReport, openBaks, closeBaks, moveBaks }
}
