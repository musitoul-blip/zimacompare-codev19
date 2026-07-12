const BASE = '/api'

async function req(method, path, body) {
  const r = await fetch(BASE + path, {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : {},
    body: body ? JSON.stringify(body) : undefined,
  })
  if (!r.ok) {
    const err = await r.json().catch(() => ({ detail: r.statusText }))
    const e = new Error(err.detail || r.statusText)
    e.status = r.status
    throw e
  }
  return r.json()
}

export const api = {
  status:        ()       => req('GET',  '/status'),
  discover:      ()       => req('GET',  '/discover'),
  validatePath:  (p)      => req('GET',  `/validate-path?path=${encodeURIComponent(p)}`),
  pathsHistory:  ()       => req('GET',  '/paths-history'),
  profiles:      ()       => req('GET',    '/profiles'),
  fileTypes:     (path)   => req('GET',    `/file-types?path=${encodeURIComponent(path)}`),
  profileSave:   (body)   => req('POST',   '/profiles', body),
  profileDelete: (name)   => req('DELETE', `/profiles/${encodeURIComponent(name)}`),
  scanStats:     ()       => req('GET',  '/scan-stats'),
  cacheStats:    ()       => req('GET',  '/cache-stats'),
  cacheClear:    ()       => req('POST', '/cache-clear'),
  scan:          (body)   => req('POST', '/scan',   body),
  scanDirs:      (p)      => req('GET',  '/scan/dirs?' + new URLSearchParams(p)),
  sync:          (body)   => req('POST', '/sync',   body),
  abort:         ()       => req('POST', '/abort'),
  reset:         ()       => req('POST', '/reset'),
  getConfig:     ()       => req('GET',  '/config'),
  setConfig:     (body)   => req('POST', '/config', body),
  auditRegistry:       ()            => req('GET',  '/audit-registry'),
  auditRegistryUpdate: (key, body)   => req('POST', `/audit-registry/${encodeURIComponent(key)}`, body),
  auditRegistryExport: ()            => req('GET',  '/audit-registry/export'),
  uiPrefGet:           (key)         => req('GET',  `/ui-prefs/${encodeURIComponent(key)}`),  // T10 Lot H
  uiPrefSet:           (key, value)  => req('POST', `/ui-prefs/${encodeURIComponent(key)}`, { value }),
  auditParams:         ()            => req('GET',  '/audit-params'),  // T10 Lot I4
  auditParamSet:       (key, value)  => req('POST', `/audit-params/${encodeURIComponent(key)}`, { value }),
  // ===== BluOS (Lot 5) =====
  bluosParams:    ()            => req('GET',  '/bluos/params'),
  bluosParamSet:  (key, value)  => req('POST', `/bluos/params/${encodeURIComponent(key)}`, { value: String(value) }),
  bluosScan:      (body)        => req('POST', '/bluos/scan', body || {}),
  bluosAbort:     ()            => req('POST', '/bluos/abort'),
  bluosStatus:    ()            => req('GET',  '/bluos/status'),
  bluosResults:   ()            => req('GET',  '/bluos/results'),
  bluosCheckPath: (path)        => req('GET',  `/bluos/check-path?path=${encodeURIComponent(path)}`),
  // ===== Cover (LOT 4) =====
  coverBluosAnalysis: (maxKb)   => req('GET',  `/cover/bluos/analysis${maxKb ? `?max_kb=${maxKb}` : ''}`),
  coverApply:         (body)    => req('POST', '/cover/apply', body),
  coverProgress:      ()        => req('GET',  '/cover/progress'),
  coverResult:        ()        => req('GET',  '/cover/result'),
  coverBaks:          ()        => req('GET',  '/cover/baks'),
  coverBaksMove:      ()        => req('POST', '/cover/baks/move'),
  coverFullUrl:       ({ path = '', folder = '', after = false, maxKb, maxPx, allowDownscale } = {}) => {
    const p = new URLSearchParams()
    if (path) p.set('path', path)
    if (folder) p.set('folder', folder)
    p.set('after', after ? 'true' : 'false')
    if (maxKb != null) p.set('max_kb', maxKb)
    if (maxPx != null) p.set('max_px', maxPx)
    if (allowDownscale != null) p.set('allow_downscale', allowDownscale ? 'true' : 'false')
    return `/api/cover/full?${p.toString()}`
  },
  coverPreviewInfo:   ({ path = '', folder = '', maxKb, maxPx, allowDownscale } = {}) => {
    const p = new URLSearchParams()
    if (path) p.set('path', path)
    if (folder) p.set('folder', folder)
    if (maxKb != null) p.set('max_kb', maxKb)
    if (maxPx != null) p.set('max_px', maxPx)
    if (allowDownscale != null) p.set('allow_downscale', allowDownscale ? 'true' : 'false')
    return req('GET', `/cover/preview-info?${p.toString()}`)
  },
  reports:       ()       => req('GET',  '/reports'),
  scanResults:   (p)      => req('GET',  `/scan-results?${new URLSearchParams(p)}`),
  diffReport:    ()       => req('GET',  '/diff-report'),
  repairPreview: (pcRoot='', kinds='read_error,content') =>
    req('GET', `/playlist/repair-preview?pc_root=${encodeURIComponent(pcRoot)}&kinds=${encodeURIComponent(kinds)}`),
  repairUrl:     (pcRoot, kinds='read_error,content') =>
    `/api/playlist/repair.m3u8?pc_root=${encodeURIComponent(pcRoot)}&kinds=${encodeURIComponent(kinds)}`,
  targetedCheck: (body)   => req('POST', '/targeted-check', body),
  targetedReport:()       => req('GET',  '/targeted-report'),
  logsRecent:    (n=300)  => req('GET',  `/logs/recent?n=${n}`),
  dependencies:  ()       => req('GET',  '/dependencies'),
  checkUpdates:  ()       => req('GET',  '/check-updates'),
  npmInfo:       (name, installed) => req('GET',
    `/npm-info?package=${encodeURIComponent(name)}${installed ? `&installed=${encodeURIComponent(installed)}` : ''}`),
  npmAudit:      (deps)   => req('POST', '/npm-audit', { deps }),
  exportContext:     ()             => req('GET',    '/export-context'),
  // NEW v3.8
  smartDevices:      ()             => req('GET',    '/smart/devices'),
  selfcheck:         ()             => req('GET',    '/selfcheck'),
  selftest:          ()             => req('GET',    '/selftest'),
  smartRefresh:      ()             => req('POST',   '/smart/refresh'),
  cleanScan:         (body)         => req('POST',   '/clean/scan', body),
  cleanPlan:         ()             => req('GET',    '/clean/plan'),
  cleanExecute:      (body)         => req('POST',   '/clean/execute', body),
  // NEW v3.12 — .zimaignore
  ignoreGet:         ()             => req('GET',    '/zimaignore'),
  ignorePut:         (content)      => req('PUT',    '/zimaignore', { content }),
  ignoreReset:       ()             => req('POST',   '/zimaignore/reset'),
  ignoreTest:        (body)         => req('POST',   '/zimaignore/test', body),
  // NEW v3.13 — liste des fichiers ignorés au dernier scan
  ignoredFiles:      (p={})         => req('GET',    `/ignored-files?${new URLSearchParams(p)}`),
  // NEW — pilotage rclone (synchro vers pCloud)
  rcloneStatus:      ()             => req('GET',    '/rclone/status'),
  rclonePing:        ()             => req('GET',    '/rclone/ping'),
  rcloneHealth:      ()             => req('GET',    '/rclone/health'),
  rcloneScanSummary: ()             => req('GET',    '/rclone/scan-summary'),
  rcloneLsd:         (path='')      => req('GET',    `/rclone/lsd${path ? `?path=${encodeURIComponent(path)}` : ''}`),
  rcloneSync:        (body)         => req('POST',   '/rclone/sync', body),
  rcloneSyncFromScan:(body)         => req('POST',   '/rclone/sync-from-scan', body),
  rcloneAbort:       ()             => req('POST',   '/rclone/abort'),
}

export function openWsLogs(onLine) {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws'
  const ws = new WebSocket(`${proto}://${location.host}/ws/logs`)
  ws.onmessage = (e) => onLine(e.data)
  ws.onerror   = () => setTimeout(() => openWsLogs(onLine), 3000)
  return ws
}

export function fmtSize(bytes) {
  if (!bytes) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let i = 0, n = bytes
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++ }
  return `${n.toFixed(1)} ${units[i]}`
}

export function fmtEta(secs) {
  if (!secs) return ''
  if (secs < 60)  return `${secs}s`
  if (secs < 3600) return `${Math.floor(secs/60)}m ${secs%60}s`
  return `${Math.floor(secs/3600)}h ${Math.floor((secs%3600)/60)}m`
}

export function fmtNum(n) {
  return (n || 0).toLocaleString('fr-FR')
}

export function fmtDateDistance(fromDate, toDate) {
  if (!fromDate || !toDate) return ''
  const a = new Date(fromDate), b = new Date(toDate)
  if (isNaN(a) || isNaN(b)) return ''
  const days = Math.abs(Math.round((b - a) / 86400000))
  if (days < 1)   return "moins d'un jour"
  if (days < 30)  return `${days} jour${days > 1 ? 's' : ''}`
  if (days < 365) {
    const months = Math.round(days / 30)
    return `${months} mois`
  }
  const years = Math.floor(days / 365)
  const remMonths = Math.round((days % 365) / 30)
  if (remMonths === 0) return `${years} an${years > 1 ? 's' : ''}`
  return `${years} an${years > 1 ? 's' : ''} ${remMonths} mois`
}

export async function copyToClipboard(text) {
  try { await navigator.clipboard.writeText(text); return true }
  catch {
    const ta = document.createElement('textarea')
    ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0'
    document.body.appendChild(ta); ta.select()
    try { document.execCommand('copy'); document.body.removeChild(ta); return true }
    catch { document.body.removeChild(ta); return false }
  }
}

export function downloadJson(data, filename) {
  const json = JSON.stringify(data, null, 2)
  const blob = new Blob([json], { type: 'application/json' })
  const url  = URL.createObjectURL(blob)
  const a    = document.createElement('a')
  a.href = url; a.download = filename
  document.body.appendChild(a); a.click()
  document.body.removeChild(a)
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}

// Heures de fonctionnement -> "3 ans 2 mois" ou "45 jours"
export function fmtPowerOnHours(hours) {
  if (hours == null) return ''
  const days = Math.floor(hours / 24)
  if (days < 30)  return `${days} jour${days > 1 ? 's' : ''}`
  if (days < 365) return `${Math.round(days/30)} mois`
  const years = Math.floor(days / 365)
  const remM  = Math.round((days % 365) / 30)
  return remM === 0 ? `${years} an${years > 1 ? 's' : ''}` : `${years} an${years > 1 ? 's' : ''} ${remM} mois`
}
