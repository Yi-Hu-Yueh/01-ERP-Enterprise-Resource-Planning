import {useEffect,useRef,useState,type FormEvent} from 'react'
import {api} from './api'
import {ActionFeedback} from './ActionFeedback'
import './FinishedGoodsReceiving.css'

type Source={completion_id:string;completion_version:number;document_no:string;work_order_id:string;work_order_no:string;
 product_id:string;product_code:string;product_name:string;unit_id:string;unit_code:string;unit_name:string;report_date:string;
 good_qty_base:string;defective_qty_base:string;received_qty_base:string;remaining_qty_base:string;lot_tracking:boolean}
type Warehouse={id:string;code:string;name:string;is_active?:boolean}
type Lot={id:string;lot_code:string}
type Policy={posting_allowed:boolean;authorization_basis:string|null;message:string}
type Draft={document_no:string;completion_id:string;completion_version:number;warehouse_id:string;business_date:string;
 quantity:string;lot_id:string;lot_code:string;note:string}
type Movement={id:string;posting_id:string;receipt_id:string;position_id:string;qty_base_delta:string;item_id:string;
 warehouse_id:string;lot_id:string|null;lot_code:string|null}
type Receipt={id:string;document_no:string;completion_id:string;completion_version:number;warehouse_id:string;business_date:string;
 quantity:string;lot_id:string|null;lot_code:string|null;note:string|null;row_version:number;work_order_id:string;product_id:string;unit_id:string;
 source:Source;warehouse_code:string;warehouse_name:string;posting_state:string;posting_id:string|null;posted_at:string|null;
 authorization_basis:string|null;current_stock_qty_base:string;movements:Movement[];created_at:string;updated_at:string;created_by:string}
type Review={receipt:Receipt;source:Source;current_stock_qty_base:string;expected_stock_qty_base:string;remaining_after_qty_base:string;
 confirmation_token:string|null;policy:Policy}
type PostBody={request_key:string;row_version:number;confirmation_token:string}
type Attempt={id:string;body:PostBody;review:Review}
type Feedback={error?:string;notice?:string}
const API='/finished-goods-receipts'
const blank=():Draft=>({document_no:'',completion_id:'',completion_version:0,warehouse_id:'',business_date:'',quantity:'',lot_id:'',lot_code:'',note:''})
const from=(r:Receipt):Draft=>({document_no:r.document_no,completion_id:r.completion_id,completion_version:r.completion_version,
 warehouse_id:r.warehouse_id,business_date:r.business_date,quantity:r.quantity,lot_id:r.lot_id??'',lot_code:r.lot_code??'',note:r.note??''})
const remember=(id?:string)=>history.replaceState(null,'','#finished-goods-receipts'+(id?'?record='+encodeURIComponent(id):''))
const unitOf=(s:Source)=>s.unit_code+' · '+s.unit_name
const positive=(value:string)=>value.startsWith('-')||value.startsWith('+')?value:'+'+value

function SourceSummary({source,prefix='fgr-source'}:{source:Source;prefix?:string}){
 const unit=unitOf(source)
 return <section className="fg-receipt-source" aria-label={prefix==='fgr-source'?'完工來源可入庫量':'過帳檢視來源可入庫量'}>
  <h3>{source.document_no} · 已確認完工回報</h3>
  <dl className="po-header-summary"><dt>製造工單</dt><dd><a href={'#work-orders?record='+source.work_order_id}>{source.work_order_no} ↗</a></dd>
   <dt>成品／基本單位</dt><dd>{source.product_code} · {source.product_name} ／ {unit}</dd>
   <dt>原完工回報日期</dt><dd>{source.report_date}</dd>
   <dt>已確認良品量</dt><dd data-testid={prefix+'-good'}>{source.good_qty_base} {unit}</dd>
   <dt>已確認不良品量</dt><dd data-testid={prefix+'-defective'}>{source.defective_qty_base} {unit}</dd>
   <dt>累計已入庫</dt><dd data-testid={prefix+'-received'}>{source.received_qty_base} {unit}</dd>
   <dt>剩餘可入庫良品量</dt><dd data-testid={prefix+'-remaining'}>{source.remaining_qty_base} {unit}</dd>
  </dl><p className="field-note">只有這張已確認回報的良品可入庫；不良品不可當成品庫存，草稿也不占用可入庫量。</p>
 </section>
}

export function FinishedGoodsReceiving({onConnection}:{onConnection:(online:boolean)=>void}){
 const [rows,setRows]=useState<Receipt[]>([]),[sources,setSources]=useState<Source[]>([]),[warehouses,setWarehouses]=useState<Warehouse[]>([]),[lots,setLots]=useState<Lot[]>([])
 const [selected,setSelected]=useState<Receipt|null>(null),[draft,setDraft]=useState<Draft>(blank),[editing,setEditing]=useState(true),[lotMode,setLotMode]=useState<'new'|'existing'>('new')
 const [query,setQuery]=useState(''),[applied,setApplied]=useState(''),[busy,setBusy]=useState(false),[loaded,setLoaded]=useState(false),[policy,setPolicy]=useState<Policy|null>(null)
 const [feedback,setFeedback]=useState<Feedback>({}),[listFeedback,setListFeedback]=useState<Feedback>({}),[sourceFeedback,setSourceFeedback]=useState<Feedback>({}),[lotFeedback,setLotFeedback]=useState<Feedback>({})
 const [rowError,setRowError]=useState<{id:string;message:string}|null>(null),[review,setReview]=useState<Review|null>(null),[reviewFeedback,setReviewFeedback]=useState<Feedback>({})
 const lock=useRef(false),createKey=useRef(crypto.randomUUID()),lotRequest=useRef(0),appliedQuery=useRef(''),attempts=useRef(new Map<string,Attempt>())
 const dialog=useRef<HTMLDialogElement>(null),reviewCancel=useRef<HTMLButtonElement>(null)
 const chosen=sources.find(s=>s.completion_id===draft.completion_id)??(selected?.completion_id===draft.completion_id?selected?.source:undefined)
 const canEdit=selected?.posting_state==='DRAFT'&&!selected.posting_id,posted=selected?.posting_state==='POSTED'
 const enter=()=>{if(lock.current)return false;lock.current=true;setBusy(true);return true}
 const leave=()=>{lock.current=false;setBusy(false)}
 async function list(q=appliedQuery.current){setRows(await api<Receipt[]>(API+'?q='+encodeURIComponent(q)));setListFeedback({});onConnection(true)}
 async function references(){
  const [s,w,p]=await Promise.all([api<Source[]>(API+'/sources'),api<Warehouse[]>('/warehouses'),api<Policy>(API+'/policy')])
  setSources(s);setWarehouses(w);setPolicy(p);setLoaded(true);onConnection(true)
 }
 async function loadLots(id:string){const key=++lotRequest.current;const values=await api<Lot[]>(API+'/lots?completion_id='+encodeURIComponent(id));if(key===lotRequest.current)setLots(values)}
 function accept(r:Receipt){setSelected(r);setDraft(from(r));setLotMode(r.lot_id?'existing':'new');if(r.posting_state==='POSTED')attempts.current.delete(r.id)}
 function fresh(){lotRequest.current++;setSelected(null);setDraft(blank());setLots([]);setLotMode('new');setEditing(true);setFeedback({});setSourceFeedback({});setLotFeedback({});setRowError(null);createKey.current=crypto.randomUUID();remember()}
 async function open(id:string){
  if(!enter())return;setFeedback({});setSourceFeedback({});setLotFeedback({});setRowError(null)
  try{const r=await api<Receipt>(API+'/'+id);accept(r);setEditing(false);remember(r.id);await Promise.all([references(),loadLots(r.completion_id)])}
  catch(e){const message=(e as Error).message;setRowError({id,message});setListFeedback({error:message})}finally{leave()}
 }
 useEffect(()=>{
  Promise.all([list(''),references()]).then(async()=>{const id=new URLSearchParams(location.hash.split('?')[1]||'').get('record');if(id)await open(id)})
   .catch(e=>{setListFeedback({error:e.message});onConnection(false)})
  const sync=()=>{if(location.hash.slice(1).split('?')[0]!=='finished-goods-receipts')return;const id=new URLSearchParams(location.hash.split('?')[1]||'').get('record');if(id)void open(id);else fresh()}
  window.addEventListener('hashchange',sync);return()=>window.removeEventListener('hashchange',sync)
 },[])
 useEffect(()=>{if(review&&!dialog.current?.open){dialog.current?.showModal();reviewCancel.current?.focus()}},[review])
 function change(key:keyof Draft,value:string){setDraft(d=>({...d,[key]:value}))}
 async function chooseSource(id:string){
  const source=sources.find(s=>s.completion_id===id);lotRequest.current++;setLots([]);setSourceFeedback({});setLotFeedback({});setLotMode('new')
  setDraft(d=>({...d,completion_id:id,completion_version:source?.completion_version??0,lot_id:'',lot_code:''}))
  if(id)try{await loadLots(id)}catch(e){setLotFeedback({error:(e as Error).message})}
 }
 function chooseLot(id:string){const lot=lots.find(l=>l.id===id);setDraft(d=>({...d,lot_id:id,lot_code:lot?.lot_code??''}))}
 function changeLotMode(mode:'new'|'existing'){setLotMode(mode);setDraft(d=>({...d,lot_id:'',lot_code:''}));setLotFeedback({})}
 function cancel(){if(selected){setDraft(from(selected));setLotMode(selected.lot_id?'existing':'new');setEditing(false);setFeedback({});setSourceFeedback({});setLotFeedback({})}else fresh()}
 async function search(e:FormEvent){e.preventDefault();setApplied(query);appliedQuery.current=query;try{await list(query)}catch(error){setListFeedback({error:(error as Error).message})}}
 async function reloadSources(){
  if(!enter())return;setSourceFeedback({})
  try{
   await references();let completionId=draft.completion_id
   if(selected&&!editing){const current=await api<Receipt>(API+'/'+selected.id);accept(current);completionId=current.completion_id}
   if(completionId)await loadLots(completionId)
   setSourceFeedback({notice:'已重新讀取來源、可入庫量、倉庫與既有成品批次；未送出異動請求，也未代為過帳。'})
  }
  catch(e){setSourceFeedback({error:(e as Error).message})}finally{leave()}
 }
 async function save(e:FormEvent){
  e.preventDefault();if(!enter())return;setFeedback({})
  try{
   if(!chosen||chosen.completion_id!==draft.completion_id)throw Error('請明確選擇既有已確認完工回報。')
   const body={...draft,lot_id:chosen.lot_tracking?(draft.lot_id||null):null,lot_code:chosen.lot_tracking?(draft.lot_code||null):null,note:draft.note||null,
    ...(selected?{row_version:selected.row_version}:{request_key:createKey.current})}
   const r=await api<Receipt>(API+(selected?'/'+selected.id:''),{method:selected?'PUT':'POST',body:JSON.stringify(body)})
   attempts.current.delete(r.id);accept(r);setEditing(false);remember(r.id);await Promise.all([list(),references(),loadLots(r.completion_id)])
   setFeedback({notice:'成品入庫草稿已儲存；未建立庫存批次、位置或分錄，也未占用可入庫良品量。'})
  }catch(error){setFeedback({error:(error as Error).message})}finally{leave()}
 }
 async function remove(){
  if(!selected||!window.confirm('確定刪除成品入庫草稿「'+selected.document_no+'」？已過帳或被引用的資料不能刪除。')||!enter())return
  setFeedback({});try{const id=selected.id;await api(API+'/'+id+'?row_version='+selected.row_version,{method:'DELETE'});attempts.current.delete(id);fresh();await Promise.all([list(),references()]);setFeedback({notice:'成品入庫草稿已刪除，來源完工回報與實體庫存未變更。'})}
  catch(e){setFeedback({error:(e as Error).message})}finally{leave()}
 }
 async function inspect(){
  if(!selected||!enter())return;setFeedback({});setReviewFeedback({})
  try{
   const current=await api<Receipt>(API+'/'+selected.id)
   if(current.posting_state==='POSTED'){accept(current);setEditing(false);await Promise.all([list(),references()]);setFeedback({notice:'此成品入庫單已過帳，已重新讀取實際分錄；沒有再次增加庫存。'});return}
   const pending=attempts.current.get(selected.id)
   if(pending){setReview(pending.review);setReviewFeedback({notice:'保留上次原請求以安全重試；取消後也可重新讀取單據，核對是否已過帳。'})}
   else{const value=await api<Review>(API+'/'+selected.id+'/posting-review');setReview(value);setPolicy(value.policy);accept(value.receipt)}
  }catch(e){setFeedback({error:(e as Error).message})}finally{leave()}
 }
 function closeReview(){if(lock.current)return;dialog.current?.close();setReview(null);setReviewFeedback({})}
 async function post(){
  if(!review?.confirmation_token||!review.policy.posting_allowed||!enter())return;setReviewFeedback({})
  const attempt=attempts.current.get(review.receipt.id)??{id:review.receipt.id,body:{request_key:crypto.randomUUID(),row_version:review.receipt.row_version,confirmation_token:review.confirmation_token},review}
  attempts.current.set(attempt.id,attempt)
  try{
   const r=await api<Receipt>(API+'/'+attempt.id+'/post',{method:'POST',body:JSON.stringify(attempt.body)})
   accept(r);setEditing(false);remember(r.id);dialog.current?.close();setReview(null)
   setFeedback({notice:'成品入庫已實際過帳；正向庫存分錄、來源可入庫量與原始連結均已提交。原料庫存未異動。'})
   try{await Promise.all([list(),references(),loadLots(r.completion_id)])}catch(e){setFeedback({error:'成品入庫已過帳，但清單重新讀取失敗：'+(e as Error).message})}
  }catch(e){setReviewFeedback({error:(e as Error).message+' 尚未取得成功回應；可保留原請求重試，或取消後重新讀取核對實際狀態。'})}finally{leave()}
 }
 const actions=<div className="form-actions" data-feedback-group><ActionFeedback {...feedback} busy={busy} onReload={selected?()=>{attempts.current.delete(selected.id);void open(selected.id)}:undefined}/>
  {editing?<button className="button primary" type="submit" disabled={busy||!loaded}>{selected?'儲存變更':'儲存入庫草稿'}</button>:canEdit&&<button className="button primary" type="button" disabled={busy} onClick={()=>{setDraft(from(selected!));setLotMode(selected!.lot_id?'existing':'new');setEditing(true);setFeedback({})}}>編輯入庫單</button>}
  {canEdit&&<button className="button danger" type="button" disabled={busy} onClick={()=>void remove()}>刪除入庫草稿</button>}
  {editing&&<button className="button" type="button" disabled={busy} onClick={cancel}>取消</button>}
  {!editing&&canEdit&&<button className="button primary" type="button" disabled={busy||!policy?.posting_allowed} onClick={()=>void inspect()}>檢視成品入庫過帳</button>}
  {selected&&<button className="button" type="button" disabled={busy} onClick={()=>{attempts.current.delete(selected.id);void open(selected.id)}}>重新讀取入庫單</button>}
 </div>
 return <>
  <div className="page-heading"><div><div className="eyebrow">MANUFACTURING / FINISHED-GOODS RECEIVING</div><h1>成品入庫管理</h1><p>選擇已確認完工回報的實際良品，另行確認成品實體入庫。</p></div><button className="button primary new-button" type="button" disabled={busy||!loaded} onClick={fresh}>＋ 新增成品入庫單</button></div>
  <div className="fg-receipt-notice" role="note"><strong>完工回報與成品入庫分開：只有明確確認本張入庫單過帳，才會增加成品庫存。</strong>
   <p>{policy?.message??'本機展示模式／尚未啟用正式電子簽核與身分權限驗證'}</p><p>不良品不得入庫；本作業不變更工單狀態，也不扣原料庫存。草稿、檢視、取消與 F5 都不會過帳。</p>
  </div>
  <div className="po-layout"><section className="panel po-list" aria-label="成品入庫單清單"><div className="panel-title"><h2>成品入庫單清單 <span className="count">{rows.length}</span></h2></div>
   <div className="search-area" data-feedback-group><form className="search-bar" onSubmit={search}><label className="sr-only" htmlFor="fgr-search">搜尋成品入庫單</label><input id="fgr-search" value={query} onChange={e=>setQuery(e.target.value)} placeholder="搜尋入庫單、完工回報、工單或成品"/><button type="submit">搜尋</button></form><ActionFeedback {...listFeedback}/></div>
   {rows.map(r=><div key={r.id} className={'po-list-row '+(selected?.id===r.id?'current-row':'')} data-feedback-group><button className="record-link" type="button" disabled={busy} aria-label={'開啟成品入庫單 '+r.document_no} onClick={()=>void open(r.id)}><span>{r.document_no} · {r.posting_state==='POSTED'?'已過帳':'草稿'}</span><strong>{r.source.product_code} · {r.source.product_name}</strong><span>{r.source.document_no} ／ {r.source.work_order_no}</span><span>{r.quantity} {r.source.unit_code} · {r.business_date}</span></button><ActionFeedback error={rowError?.id===r.id?rowError.message:''}/></div>)}
   {!rows.length&&<p className="empty-state">{applied?'沒有符合的成品入庫單。':'尚無成品入庫單；已確認完工回報不會自動建立入庫單。'}</p>}
  </section><div className="po-detail"><section className="panel" aria-label="成品入庫單資料"><div className="panel-title"><h2>{selected?.document_no??'新增成品入庫單'}</h2><span data-testid="fgr-state">{posted?'POSTED · 不可變歷史':'DRAFT · 草稿／未入庫'}</span></div>
   {editing?<form aria-label="編輯成品入庫單" onSubmit={save}><fieldset className="form-body po-fields fg-receipt-fields" disabled={busy||!loaded}>
    <div><label htmlFor="fgr-number">成品入庫單號 *</label><input id="fgr-number" required value={draft.document_no} onChange={e=>change('document_no',e.target.value)}/></div>
    <div data-feedback-group><label htmlFor="fgr-source">已確認完工回報 *</label><select id="fgr-source" required value={draft.completion_id} onChange={e=>void chooseSource(e.target.value)}><option value="">請選擇已確認完工回報</option>{sources.map(s=><option key={s.completion_id} value={s.completion_id}>{s.document_no} · {s.work_order_no} · {s.product_code} · 剩餘 {s.remaining_qty_base} {s.unit_code}</option>)}</select><ActionFeedback {...lotFeedback}/></div>
    {chosen&&<div className="po-wide"><SourceSummary source={chosen}/></div>}
    <div><label htmlFor="fgr-date">入庫日期 *</label><input id="fgr-date" type="date" required value={draft.business_date} onChange={e=>change('business_date',e.target.value)}/></div>
    <div><label htmlFor="fgr-warehouse">收貨倉庫 *</label><select id="fgr-warehouse" required value={draft.warehouse_id} onChange={e=>change('warehouse_id',e.target.value)}><option value="">請明確選取收貨倉庫</option>{warehouses.map(w=><option key={w.id} value={w.id}>{w.code} · {w.name}{w.is_active===false?'（停用）':''}</option>)}</select></div>
    <div><label htmlFor="fgr-quantity">本次入庫量 *</label><input id="fgr-quantity" type="text" inputMode="decimal" required value={draft.quantity} onChange={e=>change('quantity',e.target.value)}/></div>
    <div><label htmlFor="fgr-lot-mode">成品批次選擇</label><select id="fgr-lot-mode" value={lotMode} disabled={!chosen?.lot_tracking} onChange={e=>changeLotMode(e.target.value as 'new'|'existing')}><option value="new">新批號（過帳時建立）</option><option value="existing">選取既有製造成品批次</option></select></div>
    <div className="po-wide">{chosen?.lot_tracking?lotMode==='existing'?<><label htmlFor="fgr-lot">成品庫存批號 *</label><select id="fgr-lot" required value={draft.lot_id} onChange={e=>chooseLot(e.target.value)}><option value="">請選擇此成品的既有製造批次</option>{lots.map(l=><option key={l.id} value={l.id}>{l.lot_code}</option>)}</select></>:<><label htmlFor="fgr-lot">成品庫存批號 *</label><input id="fgr-lot" required value={draft.lot_code} onChange={e=>change('lot_code',e.target.value)}/></>:<><label htmlFor="fgr-lot-none">成品庫存批號</label><input id="fgr-lot-none" readOnly disabled value={chosen?'此成品未啟用批次管理，依實際設定不指定批次':'請先選擇完工來源'}/></>}
     <p className="field-note">既有批次的原始資料不可在此修改。新批號只於成功過帳的同一交易建立，儲存草稿不會建立批次或庫存位置。</p></div>
    <div className="po-wide"><label htmlFor="fgr-note">備註</label><textarea id="fgr-note" value={draft.note} onChange={e=>change('note',e.target.value)}/></div>
    <p className="field-note po-wide">草稿數值與過帳資格分開；實際入庫必須大於零，且不能超過原完工回報的剩餘良品量。數量與基本單位不自動換算或取位。</p>
    <p className="field-note po-wide">目前成品庫存與預計入庫後庫存，請先儲存草稿，再由「檢視成品入庫過帳」讀取後端的實際結果。</p>
   </fieldset>{actions}</form>:selected&&<><dl className="po-header-summary"><dt>成品入庫單號</dt><dd data-testid="fgr-number-value">{selected.document_no}</dd>
    <dt>完工來源</dt><dd><a href={'#production-completions?record='+selected.completion_id}>{selected.source.document_no} ↗</a> · CONFIRMED</dd>
    <dt>入庫日期</dt><dd>{selected.business_date}</dd><dt>收貨倉庫</dt><dd>{selected.warehouse_code} · {selected.warehouse_name}</dd>
    <dt>成品庫存批號</dt><dd data-testid="fgr-lot-value">{selected.lot_code??'無批次（依成品設定）'}</dd>
    <dt>本次入庫量</dt><dd data-testid="fgr-quantity-value">{selected.quantity} {unitOf(selected.source)}</dd>
    <dt>目前成品庫存</dt><dd data-testid="fgr-current-stock">{selected.current_stock_qty_base} {unitOf(selected.source)}</dd>
    <dt>備註</dt><dd>{selected.note||'—'}</dd><dt>入庫單來源識別</dt><dd className="fg-receipt-identity">{selected.id}</dd>
    <dt>過帳識別／時間</dt><dd className="fg-receipt-identity">{selected.posting_id??'未過帳'}<br/>{selected.posted_at??'—'}</dd>
    {posted&&<><dt>過帳性質</dt><dd>{selected.authorization_basis} · 本機展示人工確認，不代表正式電子簽核；已過帳資料不可直接修改或刪除。</dd></>}
   </dl>{actions}</>}
  </section>
   {!editing&&selected&&<section className="panel"><SourceSummary source={sources.find(s=>s.completion_id===selected.completion_id)??selected.source}/></section>}
   <section className="fg-receipt-refresh" data-feedback-group><button className="button" type="button" disabled={busy} onClick={()=>void reloadSources()}>重新讀取來源與倉庫</button><ActionFeedback {...sourceFeedback}/></section>
   {posted&&selected&&<section className="panel" aria-label="實際成品入庫分錄"><div className="panel-title"><h2>實際成品入庫分錄</h2></div>
    {selected.movements.map(m=><dl className="po-header-summary fg-receipt-movement" key={m.id}><dt>實際庫存異動</dt><dd data-testid="fgr-movement">{positive(m.qty_base_delta)} {unitOf(selected.source)}</dd>
     <dt>成品／收貨倉庫</dt><dd>{selected.source.product_code} ／ {selected.warehouse_code}</dd><dt>成品庫存批號</dt><dd>{m.lot_code??'無批次（依成品設定）'}</dd>
     <dt>分錄／庫存位置識別</dt><dd className="fg-receipt-identity">{m.id}<br/>{m.position_id}</dd></dl>)}
   </section>}
  </div></div>
  <dialog ref={dialog} className="fg-receipt-dialog" aria-labelledby="fgr-review-title" onCancel={e=>{e.preventDefault();closeReview()}}>
   {review&&<><h2 id="fgr-review-title">檢視成品入庫過帳</h2><p>{review.policy.message}</p><p className="fg-receipt-notice">這是成品實際入庫；確認後會寫入一筆正向庫存分錄。原完工回報保持不變，不良品不會入庫，原料庫存也不會變更。</p>
    <SourceSummary source={review.source} prefix="fgr-review-source"/>
    <dl className="po-header-summary"><dt>成品入庫單</dt><dd>{review.receipt.document_no}</dd><dt>入庫日期</dt><dd>{review.receipt.business_date}</dd>
     <dt>收貨倉庫</dt><dd>{review.receipt.warehouse_code} · {review.receipt.warehouse_name}</dd><dt>成品庫存批號</dt><dd>{review.receipt.lot_code??'無批次（依成品設定）'}</dd>
     <dt>本次入庫量</dt><dd>{review.receipt.quantity} {unitOf(review.source)}</dd><dt>預計正向異動</dt><dd>{positive(review.receipt.quantity)} {unitOf(review.source)}</dd>
     <dt>目前成品庫存</dt><dd data-testid="fgr-review-current-stock">{review.current_stock_qty_base} {unitOf(review.source)}</dd>
     <dt>預計入庫後庫存</dt><dd data-testid="fgr-expected-stock">{review.expected_stock_qty_base} {unitOf(review.source)}</dd>
     <dt>過帳後剩餘可入庫良品量</dt><dd data-testid="fgr-review-remaining-after">{review.remaining_after_qty_base} {unitOf(review.source)}</dd><dt>備註</dt><dd>{review.receipt.note||'—'}</dd>
    </dl><div className="form-actions" data-feedback-group><ActionFeedback {...reviewFeedback}/><button className="button primary" type="button" disabled={busy||!review.policy.posting_allowed||!review.confirmation_token} onClick={()=>void post()}>確認並增加成品庫存</button><button ref={reviewCancel} className="button" type="button" disabled={busy} onClick={closeReview}>取消</button></div>
   </>}
  </dialog>
 </>
}
