import {useEffect,useRef,useState,type FormEvent} from 'react'
import {api} from './api'
import {ActionFeedback} from './ActionFeedback'
import './ProductionCompletions.css'

type WorkOrder={id:string;document_no:string;row_version:number;product_id:string;product_code:string;product_name:string;
  product_base_unit_code:string;product_base_unit_name:string;planned_qty_base:string;approval_state:string;routing_id:string|null}
type Draft={document_no:string;work_order_id:string;work_order_version:number;report_date:string;good_qty_base:string;defective_qty_base:string;note:string}
type Report={id:string;document_no:string;work_order_id:string;product_id:string;unit_id:string;report_date:string;good_qty_base:string;
  defective_qty_base:string;note:string|null;row_version:number;state:string;work_order:WorkOrder;confirmation_id:string|null;
  confirmed_at:string|null;confirmed_by:string|null;authorization_basis:string|null;created_at:string;updated_at:string}
type Progress={planned_qty_base:string;confirmed_good_qty_base:string;confirmed_defective_qty_base:string;total_reported_qty_base:string;
  remaining_planned_good_qty_base:string;confirmed_report_count:number;progress_percent:string|null;progress_percent_fraction:string|null;
  progress_warning:string|null;over_plan:boolean}
type Policy={confirmation_allowed:boolean;message:string}
type Review={report:Report;current_progress:Progress;resulting_progress:Progress;policy:Policy;confirmation_token:string|null}
type Feedback={error?:string;notice?:string}
const URL='/production-completions'
const notice='完工回報僅記錄生產結果，尚未執行成品入庫，成品庫存不會因此增加。'
const blank=():Draft=>({document_no:'',work_order_id:'',work_order_version:0,report_date:'',good_qty_base:'',defective_qty_base:'',note:''})
const from=(r:Report):Draft=>({document_no:r.document_no,work_order_id:r.work_order_id,work_order_version:r.work_order.row_version,
  report_date:r.report_date,good_qty_base:r.good_qty_base,defective_qty_base:r.defective_qty_base,note:r.note??''})
const remember=(id?:string)=>window.history.replaceState(null,'',id?'#production-completions?record='+id:'#production-completions')

function ProgressView({value,unit,label}:{value:Progress;unit:string;label:string}){
 return <section className="completion-progress" aria-label={label}><h3>{label}</h3><dl className="po-header-summary">
  <dt>計畫生產量</dt><dd>{value.planned_qty_base} {unit}</dd>
  <dt>累計確認良品量</dt><dd data-testid={label+'-good'}>{value.confirmed_good_qty_base} {unit}</dd>
  <dt>累計確認不良品量</dt><dd data-testid={label+'-defective'}>{value.confirmed_defective_qty_base} {unit}</dd>
  <dt>累計回報產出</dt><dd>{value.total_reported_qty_base} {unit}</dd>
  <dt>良品達成率</dt><dd data-testid={label+'-percent'}>{value.progress_percent!==null?value.progress_percent+'%':value.progress_percent_fraction?value.progress_percent_fraction+'%（精確分數）':'未定義／無法計算'}</dd>
  <dt>尚差計畫良品量</dt><dd data-testid={label+'-remaining'}>{value.remaining_planned_good_qty_base} {unit}</dd>
  <dt>已確認回報筆數</dt><dd>{value.confirmed_report_count}</dd></dl>
  {value.over_plan&&<p className="completion-warning">累計良品已超過計畫量；如實顯示超產與負的尚差量，未限制為 100%。</p>}
  {value.progress_warning&&<p className="field-note">{value.progress_warning}</p>}
 </section>
}

export function ProductionCompletions({onConnection}:{onConnection:(online:boolean)=>void}){
 const [rows,setRows]=useState<Report[]>([]),[orders,setOrders]=useState<WorkOrder[]>([]),[selected,setSelected]=useState<Report|null>(null)
 const [draft,setDraft]=useState<Draft>(blank),[editing,setEditing]=useState(true),[query,setQuery]=useState(''),[applied,setApplied]=useState('')
 const [busy,setBusy]=useState(false),[feedback,setFeedback]=useState<Feedback>({}),[listError,setListError]=useState(''),[rowError,setRowError]=useState<{id:string;message:string}|null>(null)
 const [progress,setProgress]=useState<Progress|null>(null),[policy,setPolicy]=useState<Policy|null>(null),[review,setReview]=useState<Review|null>(null),[reviewFeedback,setReviewFeedback]=useState<Feedback>({})
 const lock=useRef(false),createKey=useRef(crypto.randomUUID()),dialog=useRef<HTMLDialogElement>(null),progressRequest=useRef(0)
 const pending=useRef<{id:string;body:{request_key:string;row_version:number;confirmation_token:string};review:Review}|null>(null)
 const chosen=orders.find(o=>o.id===draft.work_order_id)??(selected?.work_order_id===draft.work_order_id?selected?.work_order:undefined)
 const unit=chosen?chosen.product_base_unit_code+' · '+chosen.product_base_unit_name:''
 const enter=()=>{if(lock.current)return false;lock.current=true;setBusy(true);return true}
 const leave=()=>{lock.current=false;setBusy(false)}
 async function loadList(q=applied){setRows(await api<Report[]>(URL+'?q='+encodeURIComponent(q)));setListError('');onConnection(true)}
 async function loadProgress(id:string){const key=++progressRequest.current;const p=await api<Progress>(URL+'/work-orders/'+id+'/progress');if(key===progressRequest.current)setProgress(p)}
 function fresh(){progressRequest.current++;setSelected(null);setDraft(blank());setEditing(true);setProgress(null);createKey.current=crypto.randomUUID();setFeedback({});setRowError(null);remember()}
 async function open(id:string){
  setFeedback({});setRowError(null)
  try{const r=await api<Report>(URL+'/'+id);setSelected(r);setDraft(from(r));setEditing(false);remember(id);await loadProgress(r.work_order_id)}
  catch(e){setRowError({id,message:(e as Error).message});setFeedback({error:(e as Error).message})}
 }
 useEffect(()=>{
  Promise.all([loadList(''),api<WorkOrder[]>('/work-orders'),api<Policy>(URL+'/policy')]).then(async([,o,p])=>{
   setOrders(o);setPolicy(p);const id=new URLSearchParams(location.hash.split('?')[1]||'').get('record');if(id)await open(id)
  }).catch(e=>{setListError(e.message);onConnection(false)})
  const sync=()=>{if(location.hash.slice(1).split('?')[0]!=='production-completions')return;const id=new URLSearchParams(location.hash.split('?')[1]||'').get('record');if(id)void open(id);else fresh()}
  window.addEventListener('hashchange',sync);return()=>window.removeEventListener('hashchange',sync)
 },[])
 useEffect(()=>{if(review&&!dialog.current?.open)dialog.current?.showModal()},[review])
 function change(key:keyof Draft,value:string){setDraft(d=>({...d,[key]:value}))}
 function cancel(){if(selected){setDraft(from(selected));setEditing(false);setFeedback({})}else fresh()}
 async function choose(id:string){progressRequest.current++;const o=orders.find(x=>x.id===id);setDraft(d=>({...d,work_order_id:id,work_order_version:o?.row_version??0}));setProgress(null);if(id)try{await loadProgress(id)}catch(e){setFeedback({error:(e as Error).message})}}
 async function search(e:FormEvent){e.preventDefault();setApplied(query);try{await loadList(query)}catch(e){setListError((e as Error).message)}}
 async function save(e:FormEvent){
  e.preventDefault();if(!enter())return;setFeedback({})
  const was=selected
  try{
   const body={...draft,note:draft.note||null,...(was?{row_version:was.row_version}:{request_key:createKey.current})}
   const r=await api<Report>(URL+(was?'/'+was.id:''),{method:was?'PUT':'POST',body:JSON.stringify(body)})
   setSelected(r);setDraft(from(r));setEditing(false);remember(r.id);await Promise.all([loadList(),loadProgress(r.work_order_id)])
   setFeedback({notice:'完工回報草稿已儲存，尚未計入確認產出；庫存未異動。'})
  }catch(e){setFeedback({error:(e as Error).message})}finally{leave()}
 }
 async function remove(){
  if(!selected||!window.confirm('確定刪除草稿完工回報「'+selected.document_no+'」？已確認或被引用的回報無法刪除。')||!enter())return
  setFeedback({});try{await api(URL+'/'+selected.id+'?row_version='+selected.row_version,{method:'DELETE'});fresh();await loadList();setFeedback({notice:'草稿已刪除，庫存與確認產出未異動。'})}
  catch(e){setFeedback({error:(e as Error).message})}finally{leave()}
 }
 function closeReview(){if(lock.current)return;dialog.current?.close();setReview(null);setReviewFeedback({})}
 async function inspect(){
  if(!selected||!enter())return;setFeedback({})
  try{
   const current=await api<Report>(URL+'/'+selected.id)
   if(current.state==='CONFIRMED'){setSelected(current);setDraft(from(current));await Promise.all([loadProgress(current.work_order_id),loadList()]);setFeedback({notice:'此回報已確認，已重新讀取；沒有再次增加產出。'});return}
   const r=pending.current?.id===selected.id?pending.current.review:await api<Review>(URL+'/'+selected.id+'/confirmation-review')
   setReview(r);setReviewFeedback({})
  }catch(e){setFeedback({error:(e as Error).message})}finally{leave()}
 }
 async function confirm(){
  if(!review?.confirmation_token||!enter())return;setReviewFeedback({})
  const prior=pending.current?.id===review.report.id?pending.current:null
  const attempt=prior??{id:review.report.id,body:{request_key:crypto.randomUUID(),row_version:review.report.row_version,confirmation_token:review.confirmation_token},review}
  pending.current=attempt
  try{
   const r=await api<Report>(URL+'/'+attempt.id+'/confirm',{method:'POST',body:JSON.stringify(attempt.body)})
   pending.current=null;setSelected(r);setDraft(from(r));setEditing(false);remember(r.id)
   dialog.current?.close();setReview(null);await Promise.all([loadList(),loadProgress(r.work_order_id)])
   setFeedback({notice:'生產結果已確認並計入累計產出；沒有任何庫存異動。'})
  }catch(e){setReviewFeedback({error:(e as Error).message+' 若結果不明，可沿用本次確認重試；或取消後重新讀取回報。'})}finally{leave()}
 }
 const actions=<div className="form-actions" data-feedback-group><ActionFeedback {...feedback} busy={busy} onReload={selected?()=>{pending.current=null;void open(selected.id)}:undefined}/>
  {editing?<button className="button primary" type="submit" disabled={busy}>{selected?'儲存變更':'儲存回報草稿'}</button>:selected?.state==='DRAFT'&&<button className="button primary" type="button" disabled={busy} onClick={()=>{setEditing(true);setFeedback({})}}>編輯回報</button>}
  {selected?.state==='DRAFT'&&<button className="button danger" type="button" disabled={busy} onClick={()=>void remove()}>刪除回報</button>}
  {editing&&<button className="button" type="button" disabled={busy} onClick={cancel}>取消</button>}
  {!editing&&selected?.state==='DRAFT'&&<button className="button primary" type="button" disabled={busy||!policy?.confirmation_allowed} onClick={()=>void inspect()}>檢視完工確認</button>}
  {selected&&<button className="button" type="button" disabled={busy} onClick={()=>{pending.current=null;void open(selected.id)}}>重新讀取回報</button>}
 </div>
 return <>
  <div className="page-heading"><div><div className="eyebrow">MANUFACTURING / PRODUCTION COMPLETION</div><h1>生產完工回報</h1><p>分次保存實際良品與不良品，確認後形成不可變生產紀錄。</p></div><button className="button primary new-button" disabled={busy} onClick={fresh}>＋ 新增完工回報</button></div>
  <div className="completion-notice" role="note">{notice}<p>{policy?.message}</p><p>回報確認不會發放、結案或變更工單狀態；本機展示確認不代表正式電子簽核。</p></div>
  <div className="po-layout"><section className="panel po-list" aria-label="完工回報清單"><div className="panel-title"><h2>完工回報清單 <span className="count">{rows.length}</span></h2></div>
   <div className="search-area" data-feedback-group><form className="search-bar" onSubmit={search}><label className="sr-only" htmlFor="comp-search">搜尋完工回報</label><input id="comp-search" value={query} onChange={e=>setQuery(e.target.value)} placeholder="搜尋回報單號、工單、成品或備註"/><button type="submit">搜尋</button></form><ActionFeedback error={listError}/></div>
   {rows.map(r=><div key={r.id} className={'po-list-row '+(r.id===selected?.id?'current-row':'')} data-feedback-group><button className="record-link" disabled={busy} aria-label={'開啟完工回報 '+r.document_no} onClick={()=>void open(r.id)}><span>{r.document_no} · {r.state==='CONFIRMED'?'已確認':'草稿'}</span><strong>{r.work_order.document_no} · {r.work_order.product_code}</strong><span>{r.report_date} · 良品 {r.good_qty_base} ／ 不良品 {r.defective_qty_base}</span></button><ActionFeedback error={rowError?.id===r.id?rowError.message:''}/></div>)}
   {!rows.length&&<p className="empty-state">{applied?'沒有符合的回報。':'尚無完工回報；新增草稿不會增加確認產出。'}</p>}
  </section><div className="po-detail"><section className="panel" aria-label="完工回報資料"><div className="panel-title"><h2>{selected?.document_no??'新增完工回報'}</h2><span data-testid="comp-state">{selected?.state==='CONFIRMED'?'CONFIRMED · 已確認':'DRAFT · 草稿'}</span></div>
   {editing?<form aria-label="編輯完工回報" onSubmit={save}><fieldset disabled={busy} className="form-body po-fields completion-fields">
    <div><label htmlFor="comp-number">完工回報單號 *</label><input id="comp-number" required value={draft.document_no} onChange={e=>change('document_no',e.target.value)}/></div>
    <div><label htmlFor="comp-order">製造工單 *</label><select id="comp-order" required value={draft.work_order_id} onChange={e=>void choose(e.target.value)}><option value="">請選擇既有工單</option>{orders.map(o=><option key={o.id} value={o.id}>{o.document_no} · {o.product_code} · {o.product_name}</option>)}</select></div>
    <div className="po-wide"><p data-testid="comp-product">{chosen?chosen.product_code+' · '+chosen.product_name+' ／ '+unit:'請先選擇工單，成品與基本單位由來源帶入。'}</p><p className="field-note">{chosen?chosen.routing_id?'已選途程；本次為終端產出回報，不逐站重加。':'工單未選途程，仍可記錄實際產出；不補造途程。':''}</p></div>
    <div><label htmlFor="comp-date">實際回報日期 *</label><input id="comp-date" type="date" required value={draft.report_date} onChange={e=>change('report_date',e.target.value)}/></div>
    <div><label htmlFor="comp-good">本次良品量 *</label><input id="comp-good" type="text" inputMode="decimal" required value={draft.good_qty_base} onChange={e=>change('good_qty_base',e.target.value)}/></div>
    <div><label htmlFor="comp-defective">本次不良品量 *</label><input id="comp-defective" type="text" inputMode="decimal" required value={draft.defective_qty_base} onChange={e=>change('defective_qty_base',e.target.value)}/></div>
    <p className="field-note po-wide">草稿可保留有號、零與高精度數值；確認時兩項不得為負，且至少一項大於零。不良品不算良品，沒有自動取位或超產上限。</p>
    <div className="po-wide"><label htmlFor="comp-note">備註／生產說明</label><textarea id="comp-note" value={draft.note} onChange={e=>change('note',e.target.value)}/></div>
   </fieldset>{actions}</form>:selected&&<><dl className="po-header-summary">
    <dt>完工回報單號</dt><dd data-testid="comp-number-value">{selected.document_no}</dd><dt>製造工單</dt><dd><a href={'#work-orders?record='+selected.work_order_id}>{selected.work_order.document_no} ↗</a> · {selected.work_order.approval_state}</dd>
    <dt>成品與基本單位</dt><dd>{selected.work_order.product_code} · {selected.work_order.product_name} ／ {unit}</dd><dt>實際回報日期</dt><dd>{selected.report_date}</dd>
    <dt>本次良品量</dt><dd data-testid="comp-good-value">{selected.good_qty_base} {unit}</dd><dt>本次不良品量</dt><dd data-testid="comp-defective-value">{selected.defective_qty_base} {unit}</dd>
    <dt>備註／生產說明</dt><dd>{selected.note||'—'}</dd><dt>回報來源識別</dt><dd className="completion-identity">{selected.id}</dd>
    <dt>確認識別／時間</dt><dd className="completion-identity">{selected.confirmation_id??'未確認'}<br/>{selected.confirmed_at??'—'}</dd>
    {selected.state==='CONFIRMED'&&<><dt>確認性質</dt><dd>LOCAL_DEMO_MANUAL · 不可直接修改或刪除；更正流程另行核定。</dd></>}
   </dl>{actions}</>}
  </section>{progress&&<section className="panel"><ProgressView value={progress} unit={unit} label="目前工單進度"/></section>}</div></div>
  <dialog ref={dialog} className="completion-dialog" aria-labelledby="comp-review-title" onCancel={e=>{e.preventDefault();closeReview()}}>
   {review&&<><h2 id="comp-review-title">檢視完工確認</h2><p>{review.policy.message}</p><p className="completion-notice">{notice}</p>
    <dl className="po-header-summary"><dt>完工回報</dt><dd>{review.report.document_no}</dd><dt>工單／成品</dt><dd>{review.report.work_order.document_no} ／ {review.report.work_order.product_code} · {review.report.work_order.product_name}</dd>
     <dt>實際回報日期</dt><dd>{review.report.report_date}</dd><dt>本次良品量</dt><dd>{review.report.good_qty_base} {unit}</dd><dt>本次不良品量</dt><dd>{review.report.defective_qty_base} {unit}</dd><dt>備註</dt><dd>{review.report.note||'—'}</dd></dl>
    <ProgressView value={review.resulting_progress} unit={unit} label="確認後工單進度"/>
    <div className="form-actions" data-feedback-group><ActionFeedback {...reviewFeedback}/><button className="button primary" type="button" disabled={busy||!review.policy.confirmation_allowed||!review.confirmation_token} onClick={()=>void confirm()}>確認記錄生產結果</button><button className="button" type="button" disabled={busy} onClick={closeReview}>取消</button></div>
   </>}
  </dialog>
 </>
}
