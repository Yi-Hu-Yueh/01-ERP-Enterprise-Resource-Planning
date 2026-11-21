import {useEffect,useRef,useState,type FormEvent} from 'react'
import {createPortal} from 'react-dom'
import {api} from './api'
import {ActionFeedback} from './ActionFeedback'
import {ApprovalAssignees} from './ApprovalAssignees'
import {ReadonlyData,requestKey,sourceNames,useApproval,type Definition,type SourceType} from './ApprovalContext'

const templates:Record<SourceType,string>={PURCHASE_ORDER:'DEMO-PO-APPROVAL',WORK_ORDER:'DEMO-WO-APPROVAL',SALES_ORDER:'DEMO-SO-APPROVAL'}
const states:Record<string,string>={PENDING:'簽核中',APPROVED:'已同意',REJECTED:'不同意',RETURNED:'退回申請人',WITHDRAWN:'已撤回'}
type Approval={id:string;status:string;source_changed:boolean;current_stage:number;pending_assignees:{id:string;name:string}[];requester_employee_id:string;row_version:number;cycle:number;definition:Definition;available_actions:string[]}
type Preview={id:string;document_no:string;fingerprint:string;header:{row_version:number};[key:string]:unknown}
type Review={source:Preview;definition:Definition;previous:Approval|null;key:string}

export function DocumentApproval({type,id,version,blocked}:{type:SourceType;id:string;version:number;blocked:boolean}){
 const s=useApproval(),[records,setRecords]=useState<Approval[]>([]),[review,setReview]=useState<Review|null>(null),[loading,setLoading]=useState(false),[busy,setBusy]=useState(false),[error,setError]=useState(''),[notice,setNotice]=useState(''),[dialogError,setDialogError]=useState(''),[comment,setComment]=useState(''),[assignments,setAssignments]=useState<Record<string,string>>({})
 const dialog=useRef<HTMLDialogElement>(null),lock=useRef(false),sequence=useRef(0),identity=useRef('')
 const stamp=`${type}:${id}:${version}:${s.employeeId}:${blocked}`;identity.current=stamp
 const endpoint='/approval/requests?'+new URLSearchParams({employee_id:s.employeeId,view:'all',source_type:type,source_id:id})
 async function readStatus(){const captured=stamp,seq=++sequence.current;if(!s.employeeId){setRecords([]);return []}const rows=await api<Approval[]>(endpoint);if(identity.current===captured&&seq===sequence.current)setRecords(rows);return rows}
 useEffect(()=>{identity.current=stamp;setReview(null);setRecords([]);setError('');setNotice('');void readStatus().catch(e=>{if(identity.current===stamp)setError((e as Error).message)});const changed=()=>{void readStatus().catch(e=>{if(identity.current===stamp)setError((e as Error).message)})};window.addEventListener('erp:business-mutation',changed);return()=>{sequence.current++;identity.current='';window.removeEventListener('erp:business-mutation',changed)}},[stamp])
 useEffect(()=>{if(review&&!dialog.current?.open)dialog.current?.showModal()},[review])
 const active=records.find(r=>r.status==='PENDING'),latest=active||records[0]
 const mayRetry=latest?.available_actions.includes('resubmit')
 const cannotSubmit=!!active&&!mayRetry
 async function prepare(){
  if(lock.current)return;lock.current=true;setLoading(true);setError('');setNotice('');const captured=stamp
  try{
   const [sources,definitions,requests]=await Promise.all([api<Preview[]>('/approval/sources?'+new URLSearchParams({employee_id:s.employeeId,type,source_id:id})),api<Definition[]>('/approval/definitions?employee_id='+encodeURIComponent(s.employeeId)),readStatus()])
   if(identity.current!==captured)return
   const source=sources.find(v=>v.id===id);if(!source)throw new Error('目前單據已不存在，請重新讀取。')
   if(source.header.row_version!==version)throw new Error('單據資料版次已變更，請重新讀取單據後再送簽。')
   const pending=requests.find(r=>r.status==='PENDING'),previous=pending||requests[0]
   if(pending&&!pending.available_actions.includes('resubmit'))throw new Error('已有進行中的簽核，請查看簽核進度。')
   const retry=previous&&previous.available_actions.includes('resubmit')?previous:null
   if(previous&&['RETURNED','REJECTED','WITHDRAWN'].includes(previous.status)&&!retry)throw new Error('請切換原申請人，再依原申請重新送審；可由簽核歷程查看。')
   const definition=retry?retry.definition:definitions.find(d=>d.code===templates[type]&&d.source_type===type&&d.is_active)
   if(!definition)throw new Error('找不到啟用的對應簽核流程，請先檢查流程設定。')
   setComment('');setAssignments({});setDialogError('');setReview({source,definition,previous:retry,key:requestKey()})
  }catch(e){if(identity.current===captured)setError((e as Error).message)}finally{lock.current=false;setLoading(false)}
 }
 function cancel(){if(!lock.current){setReview(null);setDialogError('');setAssignments({});setComment('')}}
 async function confirm(e:FormEvent<HTMLFormElement>){
  e.preventDefault();e.stopPropagation();if(!review||lock.current||blocked)return
  lock.current=true;setBusy(true);setDialogError('');const captured=stamp
  try{
   const choices=Object.fromEntries([...new FormData(e.currentTarget)].filter(([key])=>key.startsWith("assignee:")).map(([key,value])=>[key.slice(9),String(value)]))
   const common={employee_id:s.employeeId,request_key:review.key,assignments:choices,comment:comment||null,expected_fingerprint:review.source.fingerprint}
   if(review.previous)await s.mutate('/approval/requests/'+review.previous.id+'/actions',{...common,action:'resubmit',row_version:review.previous.row_version,cycle:review.previous.cycle})
   else await s.mutate('/approval/requests',{...common,definition_id:review.definition.id,source_type:type,source_id:id})
   if(identity.current!==captured)return
   setReview(null);setNotice('送簽完成；已保存單據快照與流程版本。');await readStatus()
  }catch(e){if(identity.current===captured){setDialogError((e as Error).message);void readStatus().catch(()=>{})}}finally{lock.current=false;setBusy(false)}
 }
 return <div className="document-approval" data-testid="document-approval" data-feedback-group>
  <button className="button" type="button" onClick={()=>void prepare()} disabled={blocked||loading||busy||!s.employeeId||cannotSubmit}>送出簽核</button>
  <ActionFeedback error={error} notice={notice}/>
  {blocked&&<small>請先儲存或取消目前編輯，再送出已儲存的單據。</small>}
  {!s.employeeId&&<small>請先選擇目前模擬員工。</small>}
  {latest&&<div className="document-approval-status" role="status" data-testid="document-approval-status"><strong>{states[latest.status]||latest.status}</strong> · 第 {latest.current_stage} 關 · 待簽：{latest.pending_assignees.map(a=>a.name).join('、')||'無'}{latest.source_changed&&<span> · 來源已變更，須重新審查</span>} <a href={'#approval-history?request='+encodeURIComponent(latest.id)}>查看簽核進度／歷程</a></div>}
  {review&&createPortal(<dialog ref={dialog} className="document-approval-dialog approval-page" aria-label="確認單據送簽" onCancel={e=>{e.preventDefault();cancel()}}><form onSubmit={confirm}>
   <h2>確認單據送簽</h2><p>{sourceNames[type]}：<strong data-testid="direct-document-number">{review.source.document_no}</strong></p><p>申請人：{s.employee?.code} · {s.employee?.name}（未驗證模擬身分）</p><p data-testid="direct-template">{review.definition.name} · {review.definition.code} · v{review.definition.version}</p><p>單據版次 {review.source.header.row_version}{review.previous?' · 依原申請建立新審查次數，保留原歷程。':''}</p>
   <ApprovalAssignees definition={review.definition} requesterId={s.employeeId} assignments={assignments} onChange={setAssignments} disabled={busy}/>
   <label>申請說明<textarea aria-label="申請說明" value={comment} disabled={busy} onChange={e=>setComment(e.target.value)}/></label>
   <details><summary>檢視本次已儲存的送簽內容</summary><ReadonlyData value={review.source}/></details>
   <div className="form-actions" data-feedback-group><ActionFeedback error={dialogError}/><button type="submit" className="button primary" disabled={busy||blocked}>確認送簽</button><button type="button" className="button" disabled={busy} onClick={cancel}>取消</button></div>
  </form></dialog>,document.body)}
 </div>
}
