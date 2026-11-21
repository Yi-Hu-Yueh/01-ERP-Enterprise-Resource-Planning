import { useEffect, useRef, useState } from 'react'
import './StandaloneExport.css'

type ExportStatus = 'queued' | 'snapshotting' | 'packaging' | 'completed' | 'failed'
type ExportJob = { id: string; status: ExportStatus; message: string; error?: string | null }
type ExportConfig = { csrf_token: string; active_job_id?: string | null; latest_job_id?: string | null }
type ExportFailure = Error & { activeJobId?: string }

const endpoint = '/api/standalone-export'
const active = (job: ExportJob | null) => !!job && !['completed', 'failed'].includes(job.status)
const labels: Record<ExportStatus, string> = {
  queued: '等待匯出', snapshotting: '建立一致的 SQLite 快照', packaging: '封裝獨立系統',
  completed: '獨立系統已封裝完成', failed: '匯出失敗',
}

export const standaloneExportWarning = `此匯出將包含目前 SQLite 資料庫的
全部資料，包括客戶、供應商、採購、
製造、庫存及銷售紀錄。

匯出後的系統與目前系統各自獨立，
後續資料不會自動同步。

請妥善保管匯出的 ZIP 檔案。`

async function checkedResponse(response: Response): Promise<Response> {
  if (response.ok) return response
  const data = await response.json().catch(() => ({}))
  const message = typeof data.message === 'string' ? data.message : typeof data.detail === 'string' ? data.detail : '服務無法回應，請稍後再試。'
  const failure: ExportFailure = new Error(message)
  if (typeof data.active_job_id === 'string') failure.activeJobId = data.active_job_id
  throw failure
}

// App owns this hook so switching the keyed business workspace preserves the export job.
export function useStandaloneExport() {
  const [open, setOpen] = useState(false)
  const [job, setJob] = useState<ExportJob | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [downloading, setDownloading] = useState(false)
  const [uncertain, setUncertain] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [pollVersion, setPollVersion] = useState(0)
  const token = useRef('')
  const submissionLock = useRef(false)
  const downloadLock = useRef(false)

  async function config() {
    const response = await checkedResponse(await fetch(endpoint + '/config', { cache: 'no-store' }))
    const result: ExportConfig = await response.json()
    if (!result.csrf_token) throw new Error('無法取得匯出授權，請重新讀取後再試。')
    token.current = result.csrf_token
    return result
  }

  function followJob(id: string) {
    setJob({ id, status: 'queued', message: '正在讀取既有匯出作業，請稍候。' })
    setUncertain(false)
  }

  async function confirm() {
    if (submissionLock.current || active(job) || uncertain) return
    submissionLock.current = true
    setSubmitting(true); setError(''); setNotice('')
    let requested = false
    try {
      const settings = await config()
      if (settings.active_job_id) {
        followJob(settings.active_job_id)
        return
      }
      requested = true
      const response = await checkedResponse(await fetch(endpoint + '/jobs', {
        method: 'POST', headers: { 'Content-Type': 'application/json', 'X-ERP-Export-Token': token.current },
        body: JSON.stringify({ confirmed: true }), cache: 'no-store',
      }))
      const result: ExportJob = await response.json()
      if (!result.id || !(result.status in labels)) throw new Error('匯出作業回應不完整，請確認既有作業狀態。')
      setJob(result)
    } catch (caught) {
      const failure = caught as ExportFailure
      if (failure.activeJobId) followJob(failure.activeJobId)
      else {
        setError(failure.message)
        // A lost POST response must be recovered before offering another submission.
        if (requested) setUncertain(true)
      }
    } finally {
      submissionLock.current = false; setSubmitting(false)
    }
  }

  async function recover() {
    if (submissionLock.current) return
    submissionLock.current = true; setSubmitting(true); setError('')
    try {
      const settings = await config()
      const existing = settings.active_job_id || settings.latest_job_id
      if (existing) followJob(existing)
      else {
        setUncertain(false)
        setNotice('目前沒有進行中的匯出作業。您可再次確認匯出。')
      }
    } catch (caught) { setError((caught as Error).message) }
    finally { submissionLock.current = false; setSubmitting(false) }
  }

  const jobId = job?.id
  const running = active(job)
  useEffect(() => {
    if (!jobId || !running) return
    let stopped = false
    let timer: ReturnType<typeof setTimeout> | undefined
    const abort = new AbortController()
    async function poll() {
      try {
        const response = await checkedResponse(await fetch(endpoint + '/jobs/' + encodeURIComponent(jobId!), {
          headers: { 'X-ERP-Export-Token': token.current }, cache: 'no-store', signal: abort.signal,
        }))
        const result: ExportJob = await response.json()
        if (result.id !== jobId || !(result.status in labels)) throw new Error('匯出狀態回應不完整。')
        if (stopped) return
        setJob(result); setError('')
        if (active(result)) timer = setTimeout(poll, 1000)
      } catch (caught) {
        if (stopped) return
        setError('暫時無法讀取匯出進度：' + (caught as Error).message + ' 系統會自動重試；原作業仍保留。')
        timer = setTimeout(poll, 2000)
      }
    }
    void poll()
    return () => { stopped = true; abort.abort(); clearTimeout(timer) }
  }, [jobId, running, pollVersion])

  async function download() {
    if (!job || job.status !== 'completed' || downloadLock.current) return
    downloadLock.current = true; setDownloading(true); setError(''); setNotice('')
    try {
      const response = await checkedResponse(await fetch(endpoint + '/jobs/' + encodeURIComponent(job.id) + '/download', {
        headers: { 'X-ERP-Export-Token': token.current }, cache: 'no-store',
      }))
      const blob = await response.blob()
      if (!blob.size || !['application/zip', 'application/octet-stream', 'application/x-zip-compressed'].includes(blob.type.split(';')[0])) {
        throw new Error('未收到有效的 ZIP 檔案，請重新下載。')
      }
      const url = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = url; link.download = 'TIGER-ERP-Standalone.zip'
      document.body.appendChild(link); link.click(); link.remove()
      setTimeout(() => URL.revokeObjectURL(url), 60_000)
      setNotice('ZIP 已送至瀏覽器下載，請妥善保管檔案。')
    } catch (caught) { setError((caught as Error).message) }
    finally { downloadLock.current = false; setDownloading(false) }
  }

  function prepareAgain() { setJob(null); setError(''); setNotice(''); setUncertain(false) }
  return {
    open, setOpen, job, submitting, downloading, uncertain, error, notice, running,
    confirm, recover, download, prepareAgain, retryPoll: () => setPollVersion(value => value + 1),
  }
}

type Controller = ReturnType<typeof useStandaloneExport>

export function StandaloneExport({ controller: state, externalBusy = false }: { controller: Controller; externalBusy?: boolean }) {
  const dialog = useRef<HTMLDialogElement>(null)
  const feedback = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (state.open && !dialog.current?.open) dialog.current?.showModal()
    else if (!state.open && dialog.current?.open) dialog.current.close()
  }, [state.open])
  useEffect(() => {
    if (state.open && (state.error || state.notice)) feedback.current?.scrollIntoView({ block: 'nearest' })
  }, [state.open, state.error, state.notice])
  function close() { if (!state.submitting && !state.downloading) state.setOpen(false) }
  const hasJob = !!state.job
  return <>
    <button type="button" className="button standalone-export-trigger" disabled={externalBusy} onClick={() => state.setOpen(true)} aria-haspopup="dialog">
      匯出獨立系統{(state.running || state.submitting) && <span className="standalone-export-dot" aria-label="匯出進行中"/>}
    </button>
    <dialog ref={dialog} className="standalone-export-dialog" aria-labelledby="standalone-export-title" aria-describedby="standalone-export-warning standalone-export-sensitive" onCancel={event => { event.preventDefault(); close() }}>
      <div className="standalone-export-heading"><h2 id="standalone-export-title">匯出獨立系統</h2></div>
      <div className="standalone-export-content">
        <p id="standalone-export-warning" className="standalone-export-warning">{standaloneExportWarning}</p>
        <p id="standalone-export-sensitive" className="standalone-export-sensitive">完整資料庫也可能包含儲存在 SQLite 內的憑證或其他敏感值；匯出將保留這些資料，請妥善保管。</p>
      </div>
      <div className="standalone-export-actions" data-feedback-group>
        <div ref={feedback} className="standalone-export-feedback">
          {state.job && <div role={state.job.status === 'failed' ? 'alert' : 'status'} aria-live={state.job.status === 'failed' ? 'assertive' : 'polite'} aria-atomic="true" className={'standalone-export-progress ' + state.job.status}>
            <strong>{labels[state.job.status]}</strong><span>{state.job.error || state.job.message}</span>
          </div>}
          {state.submitting && <p role="status">正在確認匯出作業，請稍候…</p>}
          {state.error && <p role="alert" className="standalone-export-error">{state.error}</p>}
          {state.notice && <p role="status">{state.notice}</p>}
          {state.running && <p className="standalone-export-hint">可關閉此視窗並繼續瀏覽各模組，匯出作業會持續進行。</p>}
        </div>
        <div className="standalone-export-buttons">
          {!hasJob && !state.uncertain && <button type="button" className="button primary" disabled={state.submitting || externalBusy} onClick={() => void state.confirm()}>確認匯出</button>}
          {state.uncertain && <button type="button" className="button primary" disabled={state.submitting} onClick={() => void state.recover()}>重新確認作業狀態</button>}
          {state.job?.status === 'completed' && <button type="button" className="button primary" disabled={state.downloading} onClick={() => void state.download()}>{state.downloading ? '準備下載中…' : '下載 ZIP'}</button>}
          {state.running && state.error && <button type="button" className="button" onClick={state.retryPoll}>重新讀取進度</button>}
          {hasJob && !state.running && <button type="button" className="button" disabled={state.downloading} onClick={state.prepareAgain}>準備再次匯出</button>}
          <button type="button" className="button" autoFocus disabled={state.submitting || state.downloading} onClick={close}>{hasJob || state.uncertain ? '關閉' : '取消'}</button>
        </div>
      </div>
    </dialog>
  </>
}
