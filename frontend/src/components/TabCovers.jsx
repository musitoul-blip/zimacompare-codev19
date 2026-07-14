import { useState, useEffect, useMemo, useRef } from 'react'
import { api, fmtSize } from '../api.js'
import CoverPreviewModal from './CoverPreviewModal.jsx'

// ============================================================================
// Onglet Pochettes (LOT 8c) : tous les albums de la bibliotheque (pas
// seulement les fautifs BluOS), groupes par (albumartist, album) -- LOT 8a.
// Charge /api/cover/albums (sans paths[]) une fois, filtre/trie cote client,
// ne rend que les lignes filtrees. L'apercu/correction reutilise
// CoverPreviewModal (LOT 8b) ; les paths[] sont recuperes a la demande via
// /api/cover/album-paths au clic sur "Apercu" (l'album (albumartist, album)
// designe sans ambiguite possible, contrairement au matching par titre de
// /bluos/analysis).
// ============================================================================

const muted = { color: 'var(--muted)', fontSize: 13 }
const card = { border: '1px solid var(--border)', borderRadius: 8, padding: 16, marginBottom: 16 }

function keyOf(a) { return `${a.albumartist}::${a.album}` }

function SortHeader({ label, id, active, dir, onClick }) {
  return (
    <th style={{ padding: 4, cursor: 'pointer', userSelect: 'none' }} onClick={onClick}>
      {label}{active ? (dir === 'asc' ? ' ▲' : ' ▼') : ''}
    </th>
  )
}

export default function TabCovers() {
  const [albums, setAlbums] = useState(null)
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState('')

  const [search, setSearch] = useState('')
  const [minDim, setMinDim] = useState(0)
  const [minKb, setMinKb] = useState(0)
  const [sortKey, setSortKey] = useState('albumartist')
  const [sortDir, setSortDir] = useState('asc')

  const [settings, setSettings] = useState({ maxKb: 1000, maxPx: 700, allowDownscale: false })
  const [albumSettings, setAlbumSettings] = useState({})   // { [albumartist::album]: {maxKb?,maxPx?,allowDownscale?} }
  const [previewFor, setPreviewFor] = useState(null)
  const [previewLoadingKey, setPreviewLoadingKey] = useState('')
  const [previewErr, setPreviewErr] = useState('')
  const [applyBusy, setApplyBusy] = useState('')     // cle en cours, ou ''
  const [applyMsg, setApplyMsg] = useState({})       // { [cle]: message }
  const coverPollRef = useRef(null)

  async function load() {
    setLoading(true); setErr('')
    try {
      const r = await api.coverAlbums()
      setAlbums(r.albums)
    } catch (e) { setErr(e.message) } finally { setLoading(false) }
  }
  useEffect(() => { load() }, [])

  useEffect(() => () => { if (coverPollRef.current) clearInterval(coverPollRef.current) }, [])

  function effectiveSettings(k) { return { ...settings, ...(albumSettings[k] || {}) } }
  function setAlbumSetting(k, patch) { setAlbumSettings(m => ({ ...m, [k]: { ...(m[k] || {}), ...patch } })) }
  function resetAlbumSettings(k) { setAlbumSettings(m => { const c = { ...m }; delete c[k]; return c }) }

  function fmtCoverEta(secs) {
    if (!secs || secs <= 0) return ''
    if (secs < 60) return `~${secs}s`
    const m = Math.floor(secs / 60)
    const s = secs % 60
    return `~${m}min ${s}s`
  }

  function startCoverPolling(k) {
    if (coverPollRef.current) clearInterval(coverPollRef.current)
    coverPollRef.current = setInterval(async () => {
      try {
        const pg = await api.coverProgress()
        if (pg.running) {
          const eta = pg.eta_seconds > 0 ? ` — ${fmtCoverEta(pg.eta_seconds)} restantes` : ''
          setApplyMsg(m => ({ ...m, [k]: `⏳ ${pg.processed}/${pg.total}${eta}` }))
        } else {
          clearInterval(coverPollRef.current); coverPollRef.current = null
          let result = null
          try { result = await api.coverResult() } catch (e) { /* ignore */ }
          const written = result?.written ?? 0
          const errors = result?.errors ?? 0
          setApplyMsg(m => ({
            ...m,
            [k]: errors > 0 ? `✗ ${errors} erreur(s)` : `✓ terminé — ${written} fichier(s)`,
          }))
          setApplyBusy('')
        }
      } catch (e) { /* transitoire, meme logique que TabBluos */ }
    }, 1500)
  }

  async function applyCorrection(a) {
    const k = keyOf(a)
    const s = effectiveSettings(k)
    setApplyBusy(k)
    setApplyMsg(m => ({ ...m, [k]: '⏳ lancé' }))
    try {
      await api.coverApply({
        source: a.folder,
        max_kb: s.maxKb,
        max_px: s.maxPx,
        allow_downscale: s.allowDownscale,
        only_paths: a.paths || [],
      })
      startCoverPolling(k)
    } catch (e) {
      const txt = e.status === 403
        ? '⏳ Écriture désactivée (COVER_ALLOW_WRITE=false)'
        : '✗ ' + e.message
      setApplyMsg(m => ({ ...m, [k]: txt }))
      setApplyBusy('')
    }
  }

  async function openPreview(a) {
    const k = keyOf(a)
    setPreviewLoadingKey(k); setPreviewErr('')
    try {
      const { paths } = await api.coverAlbumPaths(a.albumartist, a.album)
      setPreviewFor({
        albumartist: a.albumartist, album: a.album,
        artist: a.albumartist, title: a.album,
        folder: a.folder, cover_format: a.cover_format, cover_size: a.cover_size,
        cover_width: a.cover_width, cover_height: a.cover_height,
        sample_path: a.sample_path, paths,
      })
    } catch (e) { setPreviewErr(e.message) }
    finally { setPreviewLoadingKey('') }
  }
  function closePreview() { setPreviewFor(null) }

  const filtered = useMemo(() => {
    if (!albums) return []
    const q = search.trim().toLowerCase()
    const out = albums.filter(a => {
      if (q && !`${a.albumartist} ${a.album}`.toLowerCase().includes(q)) return false
      const dim = Math.max(a.cover_width || 0, a.cover_height || 0)
      if (minDim > 0 && !(dim > minDim)) return false
      const kb = (a.cover_size || 0) / 1024
      if (minKb > 0 && !(kb > minKb)) return false
      return true
    })
    out.sort((a, b) => {
      let va, vb
      if (sortKey === 'dim') { va = Math.max(a.cover_width || 0, a.cover_height || 0); vb = Math.max(b.cover_width || 0, b.cover_height || 0) }
      else if (sortKey === 'cover_size') { va = a.cover_size || 0; vb = b.cover_size || 0 }
      else { va = (a[sortKey] || '').toLowerCase(); vb = (b[sortKey] || '').toLowerCase() }
      const r = va < vb ? -1 : va > vb ? 1 : 0
      return sortDir === 'desc' ? -r : r
    })
    return out
  }, [albums, search, minDim, minKb, sortKey, sortDir])

  function toggleSort(k) {
    if (sortKey === k) setSortDir(d => d === 'asc' ? 'desc' : 'asc')
    else { setSortKey(k); setSortDir('asc') }
  }

  return (
    <div>
      <h2 style={{ fontSize: 18, marginBottom: 4 }}>🖼 Pochettes</h2>
      <p style={muted}>Tous les albums de la bibliothèque (via master_scan.csv), groupés par artiste d'album —
        aperçu et correction indépendamment des albums signalés fautifs par BluOS.</p>

      <div style={card}>
        {loading && <div style={muted}>Chargement...</div>}
        {err && <div style={{ color: 'var(--danger)' }}>Erreur : {err}</div>}
        {albums && (
          <>
            <div style={{ display: 'flex', gap: 12, alignItems: 'center', marginBottom: 12, flexWrap: 'wrap' }}>
              <input placeholder="Rechercher artiste ou album..." value={search}
                     onChange={e => setSearch(e.target.value)}
                     style={{ width: 260, fontSize: 13 }} />
              <label style={{ fontSize: 13 }}>Dimension &gt; (px)
                <input type="number" value={minDim}
                       onChange={e => setMinDim(Number(e.target.value))}
                       style={{ width: 80, marginLeft: 4 }} />
              </label>
              <label style={{ fontSize: 13 }}>Poids &gt; (Ko)
                <input type="number" value={minKb}
                       onChange={e => setMinKb(Number(e.target.value))}
                       style={{ width: 80, marginLeft: 4 }} />
              </label>
              <span style={muted}>{filtered.length} / {albums.length} albums</span>
            </div>

            <table style={{ width: '100%', fontSize: 13, borderCollapse: 'collapse' }}>
              <thead>
                <tr style={{ textAlign: 'left', color: 'var(--muted)' }}>
                  <SortHeader label="Artiste" id="albumartist" active={sortKey === 'albumartist'} dir={sortDir} onClick={() => toggleSort('albumartist')} />
                  <SortHeader label="Album" id="album" active={sortKey === 'album'} dir={sortDir} onClick={() => toggleSort('album')} />
                  <th style={{ padding: 4 }}>Format</th>
                  <SortHeader label="Dimensions" id="dim" active={sortKey === 'dim'} dir={sortDir} onClick={() => toggleSort('dim')} />
                  <SortHeader label="Poids" id="cover_size" active={sortKey === 'cover_size'} dir={sortDir} onClick={() => toggleSort('cover_size')} />
                  <th style={{ padding: 4 }}>Pistes</th>
                  <th style={{ padding: 4 }}></th>
                </tr>
              </thead>
              <tbody>
                {filtered.map(a => {
                  const k = keyOf(a)
                  return (
                    <tr key={k} style={{ borderTop: '1px solid var(--border)' }}>
                      <td style={{ padding: 4 }}>{a.albumartist}</td>
                      <td style={{ padding: 4 }}>{a.album}</td>
                      <td style={{ padding: 4, ...muted }}>{a.cover_format || '—'}</td>
                      <td style={{ padding: 4, ...muted }}>{a.cover_width && a.cover_height ? `${a.cover_width}×${a.cover_height}` : ''}</td>
                      <td style={{ padding: 4, ...muted }}>{a.cover_size ? fmtSize(a.cover_size) : ''}</td>
                      <td style={{ padding: 4, ...muted }}>{a.nb_tracks}</td>
                      <td style={{ padding: 4 }}>
                        {a.folder && (
                          <button onClick={() => openPreview(a)} disabled={previewLoadingKey === k}
                                  style={{ fontSize: 12, padding: '2px 8px' }}>
                            {previewLoadingKey === k ? '...' : '👁 Aperçu'}
                          </button>
                        )}
                        {applyMsg[k] && <span style={{ fontSize: 12, marginLeft: 6 }}>{applyMsg[k]}</span>}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
            {previewErr && <div style={{ color: 'var(--danger)', fontSize: 13, marginTop: 8 }}>✗ {previewErr}</div>}
          </>
        )}
      </div>

      {previewFor && (() => {
        const k = keyOf(previewFor)
        return (
          <CoverPreviewModal
            album={previewFor}
            settings={effectiveSettings(k)}
            onSettingChange={patch => setAlbumSetting(k, patch)}
            onReset={() => resetAlbumSettings(k)}
            busy={applyBusy === k}
            message={applyMsg[k]}
            onApply={() => applyCorrection(previewFor)}
            onClose={closePreview}
          />
        )
      })()}
    </div>
  )
}
