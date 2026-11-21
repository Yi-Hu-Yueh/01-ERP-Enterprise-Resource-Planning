import { useEffect, useRef, useState } from 'react'
import './SQLiteMaintenance.css'

type Backup = {
  id: string; created_at: string; bytes: number; sha256: string; row_count: number;
  business_row_count: number; table_count: number; schema_version: string;
  database_id: string; integrity_status: string; kind: 'backup' | 'recovery';
}
type DatabaseSummary = {
  id: string; filename: string; path: string; schema_version: string;
  row_count: number; business_row_count: number; technical_preserved: string[];
}
type Result = { operation: string; success: boolean; committed?: boolean; warning?: string; message: string; completed_at: string }
type Status = {
  csrf_token: string; backups: Backup[]; clear_eligible: boolean; clear_disabled_reason: string;
  restore_available: boolean; busy: boolean; operation: string | null; last_result: Result | null;
  database: DatabaseSummary;
}
type Review = { confirmation_token: string; expires_in: number; summary: { database: DatabaseSummary; backup: Backup } }
type Operation = 'clear' | 'restore'
const endpoint = '/api/sqlite-maintenance'
const date = (value: string) => new Date(value).toLocaleString('zh-TW', { hour12: false })
const bytes = (value: number) => value.toLocaleString('zh-TW') + ' 位元組'

async function readResponse<T>(response: Response): Promise<T> {
  const data = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(typeof data.message === 'string' ? data.message : '維護服務無法回應，請重新讀取狀態。')
  return data as T
}

// App owns the controller: navigation cannot reset backend authority or lose an operation result.
export function useSQLiteMaintenance(onDatabaseReplaced: () => void) {
  const [status, setStatus] = useState<Status | null>(null)
  const [verified, setVerified] = useState(false)
  const [error, setError] = useState('')
  const [statusError, setStatusError] = useState('')
  const [mode, setMode] = useState<Operation | null>(null)
  const [selected, setSelected] = useState('')
  const [review, setReview] = useState<Review | null>(null)
  const [reviewing, setReviewing] = useState(false)
  const [dialogError, setDialogError] = useState('')
  const [confirmation, setConfirmation] = useState('')
  const [submitting, setSubmitting] = useState<'backup' | Operation | null>(null)
  const statusSequence = useRef(0)
  const reviewSequence = useRef(0)
  const lock = useRef(false)
  const mounted = useRef(true)
  const token = useRef('')
  const previousReplacement = useRef<string | null | undefined>(undefined)
  const replaced = useRef(onDatabaseReplaced)
  replaced.current = onDatabaseReplaced

  function acceptStatus(value: Status) {
    if (!value.csrf_token || !value.database || !Array.isArray(value.backups)) throw new Error('維護狀態回應不完整；請重新讀取。')
    token.current = value.csrf_token
    if (!mounted.current) return
    const result = value.last_result
    // A verified commit changes business state even when later metadata I/O warns.
    setStatus(result?.committed && result.warning ? { ...value, clear_eligible: false } : value)
    setVerified(true); setStatusError('')
    const replacement = (result?.success || result?.committed) && ['clear', 'restore'].includes(result.operation)
      ? result.operation + ':' + result.completed_at : null
    if (replacement && previousReplacement.current !== undefined && previousReplacement.current !== replacement) {
      setMode(null); setReview(null); setDialogError(''); setConfirmation('')
      replaced.current()
    }
    if (replacement || previousReplacement.current === undefined) previousReplacement.current = replacement
  }

  async function refresh() {
    const sequence = ++statusSequence.current
    try {
      const value = await readResponse<Status>(await fetch(endpoint + '/status', { cache: 'no-store' }))
      if (sequence === statusSequence.current) acceptStatus(value)
      return value
    } catch (caught) {
      if (mounted.current && sequence === statusSequence.current) {
        setVerified(false); setStatusError((caught as Error).message)
      }
      throw caught
    }
  }

  async function post<T>(route: string, body: unknown) {
    return readResponse<T>(await fetch(endpoint + route, {
      method: 'POST', cache: 'no-store', headers: { 'Content-Type': 'application/json', 'X-ERP-Maintenance-Token': token.current },
      body: JSON.stringify(body),
    }))
  }

  useEffect(() => {
    mounted.current = true
    const reload = () => { void refresh().catch(() => {}) }
    const mutation = () => { setVerified(false); reload() }
    reload()
    const timer = setInterval(reload, 2000)
    window.addEventListener('hashchange', reload)
    window.addEventListener('focus', reload)
    window.addEventListener('erp:business-mutation', mutation)
    return () => {
      mounted.current = false; ++statusSequence.current; ++reviewSequence.current; clearInterval(timer)
      window.removeEventListener('hashchange', reload)
      window.removeEventListener('focus', reload)
      window.removeEventListener('erp:business-mutation', mutation)
    }
  }, [])

  async function backup() {
    if (lock.current) return
    lock.current = true; setSubmitting('backup'); setError('')
    try {
      await refresh()
      const result = await post<{ status: Status }>('/backups', {})
      ++statusSequence.current; acceptStatus(result.status)
    } catch (caught) { setError((caught as Error).message); setVerified(false) }
    finally { lock.current = false; setSubmitting(null); void refresh().catch(() => {}) }
  }

  async function loadReview(operation: Operation, backupId: string) {
    const sequence = ++reviewSequence.current
    setReview(null); setDialogError(''); setReviewing(true)
    try {
      await refresh()
      const value = await post<Review>('/review', { operation, ...(operation === 'restore' ? { backup_id: backupId } : {}) })
      if (!value.confirmation_token || !value.summary?.database || !value.summary.backup) throw new Error('確認資料不完整；請重新確認。')
      if (mounted.current && sequence === reviewSequence.current) setReview(value)
    } catch (caught) {
      if (mounted.current && sequence === reviewSequence.current) setDialogError((caught as Error).message)
    } finally { if (mounted.current && sequence === reviewSequence.current) setReviewing(false) }
  }

  function open(operation: Operation) {
    if (lock.current) return
    const backupId = status?.backups.find(item => item.kind === 'backup')?.id || status?.backups[0]?.id || ''
    setMode(operation); setSelected(backupId); setConfirmation(''); setDialogError('')
    void loadReview(operation, backupId)
  }
  function cancel() {
    if (lock.current) return
    ++reviewSequence.current; setMode(null); setReview(null); setReviewing(false); setDialogError(''); setConfirmation('')
  }
  function selectBackup(id: string) { setSelected(id); if (mode) void loadReview(mode, id) }

  async function confirm() {
    if (lock.current || !mode || !review || (mode === 'clear' && confirmation !== '清空')) return
    const operation = mode
    lock.current = true; setSubmitting(operation); setDialogError(''); setError('')
    try {
      const value = await post<{ status: Status }>('/' + operation, operation === 'clear'
        ? { confirmation: '清空', confirmation_token: review.confirmation_token }
        : { backup_id: selected, confirmed: true, confirmation_token: review.confirmation_token })
      setMode(null); setReview(null); setConfirmation('')
      ++statusSequence.current; acceptStatus(value.status)
    } catch (caught) {
      setDialogError((caught as Error).message); setReview(null); setVerified(false)
    } finally { lock.current = false; setSubmitting(null); void refresh().catch(() => {}) }
  }

  return {
    status, verified, error: error || statusError, mode, selected, review, reviewing, dialogError, confirmation, setConfirmation,
    submitting, busy: !!submitting || !!status?.busy, backup, open, cancel, selectBackup, confirm,
    blocksExport: !!submitting || (!!status?.busy && status.operation !== 'export'),
    retry: () => { setError(''); void refresh().catch(() => {}) }, retryReview: () => { if (mode) void loadReview(mode, selected) },
  }
}

type Controller = ReturnType<typeof useSQLiteMaintenance>

function DatabaseDetails({ database }: { database: DatabaseSummary }) {
  return <dl className="sqlite-maintenance-summary">
    <dt>目前資料庫</dt><dd>{database.filename}</dd>
    <dt>資料位置</dt><dd>{database.path}</dd>
    <dt>資料庫識別</dt><dd>{database.id}</dd>
    <dt>結構版本</dt><dd>{database.schema_version}</dd>
    <dt>業務筆數</dt><dd>{database.business_row_count.toLocaleString('zh-TW')} 筆（總計 {database.row_count.toLocaleString('zh-TW')} 筆）</dd>
  </dl>
}

function BackupDetails({ backup }: { backup: Backup }) {
  return <dl className="sqlite-maintenance-summary" aria-label="已驗證備份資訊">
    <dt>備份時間</dt><dd>{date(backup.created_at)}{backup.kind === 'recovery' ? '（恢復前保護快照）' : ''}</dd>
    <dt>檔案大小</dt><dd>{bytes(backup.bytes)}</dd>
    <dt>完整性</dt><dd>{backup.integrity_status === 'VERIFIED' ? '已驗證' : backup.integrity_status}</dd>
    <dt>記錄筆數</dt><dd>業務 {backup.business_row_count.toLocaleString('zh-TW')} 筆／全部 {backup.row_count.toLocaleString('zh-TW')} 筆</dd>
    <dt>資料庫識別</dt><dd>{backup.database_id}</dd>
    <dt>結構版本</dt><dd>{backup.schema_version}／{backup.table_count} 個資料表</dd>
  </dl>
}

export function SQLiteMaintenance({ controller: state, externalBusy = false }: { controller: Controller; externalBusy?: boolean }) {
  const dialog = useRef<HTMLDialogElement>(null)
  const dialogFeedback = useRef<HTMLDivElement>(null)
  const toolbarFeedback = useRef<HTMLDivElement>(null)
  const busy = state.busy || externalBusy
  const ready = state.verified && !!state.status && !busy
  const clearReason = !state.verified ? '正在確認備份資格；無法確認時禁止清空。' : state.status?.clear_disabled_reason || ''
  const summary = state.review?.summary
  const operationName = state.mode === 'clear' ? '清空 SQLite' : '恢復 SQLite'
  useEffect(() => {
    if (state.mode && !dialog.current?.open) dialog.current?.showModal()
    else if (!state.mode && dialog.current?.open) dialog.current.close()
  }, [state.mode])
  useEffect(() => {
    if (state.dialogError) dialogFeedback.current?.scrollIntoView({ block: 'nearest' })
  }, [state.dialogError])
  useEffect(() => {
    if (state.error || state.submitting || state.status?.last_result?.completed_at) toolbarFeedback.current?.scrollIntoView({ block: 'nearest' })
  }, [state.error, state.submitting, state.status?.last_result?.completed_at])

  return <div className="sqlite-maintenance">
    <div className="sqlite-maintenance-buttons" role="group" aria-label="SQLite 資料維護" aria-busy={busy}>
      <button type="button" className="button sqlite-maintenance-trigger" disabled={!ready} onClick={() => void state.backup()}>{state.submitting === 'backup' ? '備份驗證中…' : '備份 SQLite'}</button>
      <button type="button" className="button danger sqlite-maintenance-trigger" disabled={!ready || !state.status?.clear_eligible} aria-describedby="sqlite-maintenance-clear-reason" title={clearReason} aria-haspopup="dialog" onClick={() => state.open('clear')}>清空 SQLite</button>
      <button type="button" className="button sqlite-maintenance-trigger" disabled={!ready || !state.status?.restore_available} aria-haspopup="dialog" onClick={() => state.open('restore')}>恢復 SQLite</button>
    </div>
    <div ref={toolbarFeedback} className="sqlite-maintenance-feedback" aria-atomic="true">
      {state.error && <p role="alert" className="sqlite-maintenance-error">{state.error} <button type="button" disabled={!!state.submitting} onClick={state.retry}>重新讀取狀態</button></p>}
      {busy ? <p role="status">{state.submitting === 'backup' ? '正在建立並驗證完整備份，請稍候。' : state.submitting === 'clear' ? '正在清空並驗證資料，請稍候。' : state.submitting === 'restore' ? '正在保護目前資料並驗證恢復結果，請稍候。' : '資料維護或匯出作業進行中，請稍候。'}</p>
        : state.status?.last_result && <p role={state.status.last_result.warning || !state.status.last_result.success ? 'alert' : 'status'} className={state.status.last_result.warning || !state.status.last_result.success ? 'sqlite-maintenance-error' : ''}>{state.status.last_result.committed && state.status.last_result.warning && <strong>資料已提交並通過驗證。 </strong>}{state.status.last_result.message}{state.status.last_result.warning && <> 警告：{state.status.last_result.warning}</>}</p>}
      <p id="sqlite-maintenance-clear-reason">{clearReason}</p>
    </div>
    <dialog ref={dialog} className="sqlite-maintenance-dialog" aria-labelledby="sqlite-maintenance-title" aria-describedby="sqlite-maintenance-warning" onCancel={event => { event.preventDefault(); state.cancel() }}>
      <div className="sqlite-maintenance-heading"><h2 id="sqlite-maintenance-title">{operationName}</h2></div>
      <div className="sqlite-maintenance-content">
        <p id="sqlite-maintenance-warning" className={state.mode === 'clear' ? 'sqlite-maintenance-warning danger-text' : 'sqlite-maintenance-warning'}>
          {state.mode === 'clear' ? '這會清空目前執行中 ERP 資料庫的全部業務資料，包含主檔、採購、銷售、製造與庫存紀錄。資料表及必要技術結構會保留。已驗證備份仍會保留，可透過「恢復 SQLite」取回資料。此操作只影響下列目前資料庫，不會清空電腦上的其他 SQLite 資料庫。'
            : '恢復會以所選備份取代目前 ERP 的資料狀態。該備份之後新增或修改的紀錄可能被取代。系統會先建立並驗證另一份受保護的恢復前快照，保留目前狀態，不會覆寫原始備份。'}
        </p>
        {state.mode === 'restore' && <><label className="sqlite-maintenance-label" htmlFor="sqlite-maintenance-backup">選擇已驗證備份</label>
          <select id="sqlite-maintenance-backup" value={state.selected} disabled={!!state.submitting} onChange={event => state.selectBackup(event.target.value)}>
            {state.status?.backups.map(item => <option key={item.id} value={item.id}>{date(item.created_at)} · {item.kind === 'recovery' ? '恢復前快照' : '備份'} · {item.business_row_count} 筆業務資料 · {bytes(item.bytes)}</option>)}
          </select></>}
        {summary && <><DatabaseDetails database={summary.database}/><h3>{state.mode === 'clear' ? '本次清空的保護備份' : '選取的恢復來源'}</h3><BackupDetails backup={summary.backup}/>
          {state.mode === 'clear' && <p className="sqlite-maintenance-note">清空後保留的必要技術資料：{summary.database.technical_preserved.join('、') || '無業務資料例外'}。清空或恢復後，再次清空前必須重新成功備份。</p>}
        </>}
        {state.mode === 'clear' && <><label className="sqlite-maintenance-label" htmlFor="sqlite-maintenance-confirmation">請輸入「清空」以確認</label><input id="sqlite-maintenance-confirmation" value={state.confirmation} disabled={!!state.submitting} autoComplete="off" onChange={event => state.setConfirmation(event.target.value)}/></>}
      </div>
      <div className="sqlite-maintenance-actions" data-feedback-group>
        <div ref={dialogFeedback} className="sqlite-maintenance-feedback">
          {state.reviewing && <p role="status">正在重新驗證目前資料庫與備份…</p>}
          {state.dialogError && <p role="alert" className="sqlite-maintenance-error">{state.dialogError}</p>}
          {!!state.submitting && <p role="status">{state.mode === 'clear' ? '正在清空並驗證，請勿重複送出。' : '正在建立保護快照並恢復，請勿重複送出。'}</p>}
        </div>
        <div className="sqlite-maintenance-dialog-buttons">
          <button type="button" className={'button ' + (state.mode === 'clear' ? 'danger' : 'primary')} disabled={busy || !state.verified || state.reviewing || !state.review || (state.mode === 'clear' && (state.confirmation !== '清空' || !state.status?.clear_eligible))} onClick={() => void state.confirm()}>{state.mode === 'clear' ? '確認清空' : '確認恢復'}</button>
          {!state.review && !state.reviewing && <button type="button" className="button" disabled={busy} onClick={state.retryReview}>重新確認</button>}
          <button type="button" className="button" autoFocus disabled={!!state.submitting} onClick={state.cancel}>取消</button>
        </div>
      </div>
    </dialog>
  </div>
}
