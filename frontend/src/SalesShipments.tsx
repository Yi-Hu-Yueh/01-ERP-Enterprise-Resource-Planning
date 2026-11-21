import {useEffect,useRef,useState,type FormEvent} from 'react'
import {api} from './api'
import {ActionFeedback} from './ActionFeedback'
import './FinishedGoodsReceiving.css'

type Source={source_line_id:string;line_no:number;line_version:number;order_version:number;sales_order_id:string;sales_order_no:string;
 customer_id:string;customer_code:string;customer_name:string;product_id:string;product_code:string;product_name:string;
 unit_id:string;unit_code:string;unit_name:string;ordered_qty_base:string;shipped_qty_base:string;remaining_qty_base:string;lot_tracking:boolean}
type Position={id:string;warehouse_id:string;warehouse_code:string;warehouse_name:string;lot_id:string|null;lot_code:string|null;on_hand_qty_base:string}
type Policy={posting_allowed:boolean;authorization_basis:string|null;message:string}
type Draft={document_no:string;sales_order_line_id:string;line_version:number;order_version:number;position_id:string;business_date:string;quantity:string;note:string}
type Movement={id:string;posting_id:string;shipment_id:string;position_id:string;qty_base_delta:string;item_id:string;warehouse_id:string;lot_id:string|null;lot_code:string|null}
type Shipment=Omit<Draft,'note'>&{id:string;note:string|null;row_version:number;sales_order_id:string;customer_id:string;product_id:string;unit_id:string;
 warehouse_id:string;lot_id:string|null;lot_code:string|null;source:Source;warehouse_code:string;warehouse_name:string;posting_state:string;
 posting_id:string|null;posted_at:string|null;authorization_basis:string|null;current_stock_qty_base:string;movements:Movement[];created_at:string;updated_at:string;created_by:string}
type Review={shipment:Shipment;source:Source;current_stock_qty_base:string;expected_stock_qty_base:string;remaining_after_qty_base:string;confirmation_token:string|null;policy:Policy}
type PostBody={request_key:string;row_version:number;confirmation_token:string}
type Attempt={id:string;body:PostBody;review:Review}
type Feedback={error?:string;notice?:string}
const API='/sales-shipments'
const blank=():Draft=>({document_no:'',sales_order_line_id:'',line_version:0,order_version:0,position_id:'',business_date:'',quantity:'',note:''})
const from=(r:Shipment):Draft=>({document_no:r.document_no,sales_order_line_id:r.sales_order_line_id,line_version:r.line_version,order_version:r.order_version,
 position_id:r.position_id,business_date:r.business_date,quantity:r.quantity,note:r.note??''})
const remember=(id?:string)=>history.replaceState(null,'','#sales-shipments'+(id?'?record='+encodeURIComponent(id):''))
const unitOf=(s:Source)=>s.unit_code+' · '+s.unit_name

function SourceSummary({source,prefix='ss-source'}:{source:Source;prefix?:string}){
 const unit=unitOf(source)
 return <section className="fg-receipt-source" aria-label={prefix==='ss-source'?'來源銷售明細與未出貨量':'過帳檢視來源與未出貨量'}>
  <h3>來源銷售訂單 · {source.sales_order_no}</h3><dl className="po-header-summary">
   <dt>來源銷售訂單</dt><dd><a href={'#sales-orders?record='+source.sales_order_id}>{source.sales_order_no} ↗</a></dd>
   <dt>客戶</dt><dd data-testid={prefix+'-customer'}>{source.customer_code} · {source.customer_name}</dd>
   <dt>成品／基本單位</dt><dd>{source.product_code} · {source.product_name} ／ {unit}</dd>
   <dt>訂購數量</dt><dd data-testid={prefix+'-ordered'}>{source.ordered_qty_base} {unit}</dd>
   <dt>累計已出貨量</dt><dd data-testid={prefix+'-shipped'}>{source.shipped_qty_base} {unit}</dd>
   <dt>剩餘未出貨量</dt><dd data-testid={prefix+'-remaining'}>{source.remaining_qty_base} {unit}</dd>
  </dl><p className="field-note">只計算這筆銷售明細的實際已過帳出貨；草稿不占用訂購量，也不改變原訂單金額。</p>
 </section>
}

export function SalesShipments({onConnection}:{onConnection:(online:boolean)=>void}){
 const [rows,setRows]=useState<Shipment[]>([]),[sources,setSources]=useState<Source[]>([]),[positions,setPositions]=useState<Position[]>([])
 const [selected,setSelected]=useState<Shipment|null>(null),[draft,setDraft]=useState<Draft>(blank),[editing,setEditing]=useState(true),[warehouse,setWarehouse]=useState('')
 const [query,setQuery]=useState(''),[applied,setApplied]=useState(''),[busy,setBusy]=useState(false),[loaded,setLoaded]=useState(false),[policy,setPolicy]=useState<Policy|null>(null)
 const [feedback,setFeedback]=useState<Feedback>({}),[listFeedback,setListFeedback]=useState<Feedback>({}),[sourceFeedback,setSourceFeedback]=useState<Feedback>({}),[positionFeedback,setPositionFeedback]=useState<Feedback>({})
 const [rowError,setRowError]=useState<{id:string;message:string}|null>(null),[review,setReview]=useState<Review|null>(null),[reviewFeedback,setReviewFeedback]=useState<Feedback>({})
 const lock=useRef(false),createKey=useRef(crypto.randomUUID()),positionRequest=useRef(0),appliedQuery=useRef(''),attempts=useRef(new Map<string,Attempt>())
 const dialog=useRef<HTMLDialogElement>(null),reviewCancel=useRef<HTMLButtonElement>(null)
 const chosen=sources.find(s=>s.source_line_id===draft.sales_order_line_id)??(selected?.sales_order_line_id===draft.sales_order_line_id?selected.source:undefined)
 const chosenPosition=positions.find(p=>p.id===draft.position_id)
 const warehouses=positions.filter((p,i,all)=>all.findIndex(x=>x.warehouse_id===p.warehouse_id)===i)
 const canEdit=selected?.posting_state==='DRAFT'&&!selected.posting_id,posted=selected?.posting_state==='POSTED'
 const enter=()=>{if(lock.current)return false;lock.current=true;setBusy(true);return true}
 const leave=()=>{lock.current=false;setBusy(false)}
 async function list(q=appliedQuery.current){setRows(await api<Shipment[]>(API+'?q='+encodeURIComponent(q)));setListFeedback({});onConnection(true)}
 async function references(){const [s,p]=await Promise.all([api<Source[]>(API+'/sources'),api<Policy>(API+'/policy')]);setSources(s);setPolicy(p);setLoaded(true);onConnection(true)}
 async function loadPositions(id:string){const request=++positionRequest.current;const result=await api<Position[]>(API+'/positions?sales_order_line_id='+encodeURIComponent(id));if(request===positionRequest.current)setPositions(result)}
 function accept(r:Shipment){setSelected(r);setDraft(from(r));setWarehouse(r.warehouse_id);if(r.posting_state==='POSTED')attempts.current.delete(r.id)}
 function fresh(){positionRequest.current++;setSelected(null);setDraft(blank());setPositions([]);setWarehouse('');setEditing(true);setFeedback({});setSourceFeedback({});setPositionFeedback({});setRowError(null);createKey.current=crypto.randomUUID();remember()}
 async function open(id:string){if(!enter())return;setFeedback({});setSourceFeedback({});setPositionFeedback({});setRowError(null)
  try{const r=await api<Shipment>(API+'/'+id);accept(r);setEditing(false);remember(r.id);await Promise.all([references(),loadPositions(r.sales_order_line_id)])}
  catch(e){const message=(e as Error).message;setRowError({id,message});setListFeedback({error:message})}finally{leave()}}
 useEffect(()=>{
  Promise.all([list(''),references()]).then(async()=>{const id=new URLSearchParams(location.hash.split('?')[1]||'').get('record');if(id)await open(id)}).catch(e=>{setListFeedback({error:e.message});onConnection(false)})
  const sync=()=>{if(location.hash.slice(1).split('?')[0]!=='sales-shipments')return;const id=new URLSearchParams(location.hash.split('?')[1]||'').get('record');if(id)void open(id);else fresh()}
  window.addEventListener('hashchange',sync);return()=>window.removeEventListener('hashchange',sync)
 },[])
 useEffect(()=>{if(review&&!dialog.current?.open){dialog.current?.showModal();reviewCancel.current?.focus()}},[review])
 function change(key:keyof Draft,value:string){setDraft(d=>({...d,[key]:value}))}
 async function chooseSource(id:string){const s=sources.find(s=>s.source_line_id===id);positionRequest.current++;setPositions([]);setWarehouse('');setSourceFeedback({});setPositionFeedback({});setDraft(d=>({...d,sales_order_line_id:id,line_version:s?.line_version??0,order_version:s?.order_version??0,position_id:''}));if(id)try{await loadPositions(id)}catch(e){setPositionFeedback({error:(e as Error).message})}}
 function chooseWarehouse(id:string){setWarehouse(id);setDraft(d=>({...d,position_id:''}));setPositionFeedback({})}
 function cancel(){if(selected){setDraft(from(selected));setWarehouse(selected.warehouse_id);setEditing(false);setFeedback({});setSourceFeedback({});setPositionFeedback({})}else fresh()}
 useEffect(()=>{const escape=(e:KeyboardEvent)=>{if(e.key==='Escape'&&!lock.current&&!dialog.current?.open&&editing)cancel()};window.addEventListener('keydown',escape);return()=>window.removeEventListener('keydown',escape)},[editing,selected])
 async function search(e:FormEvent){e.preventDefault();setApplied(query);appliedQuery.current=query;try{await list(query)}catch(e){setListFeedback({error:(e as Error).message})}}
 async function reloadSources(){if(!enter())return;setSourceFeedback({});try{await references();let id=draft.sales_order_line_id;if(selected&&!editing){const current=await api<Shipment>(API+'/'+selected.id);accept(current);id=current.sales_order_line_id}if(id)await loadPositions(id);setSourceFeedback({notice:'已重新讀取來源、未出貨量與實際庫存；未送出異動請求，也未代為出貨。'})}catch(e){setSourceFeedback({error:(e as Error).message})}finally{leave()}}
 async function save(e:FormEvent){e.preventDefault();if(!enter())return;setFeedback({});try{
  if(!chosen||!chosenPosition)throw Error('請選取實際銷售明細、出貨倉庫與既有庫存批次位置。')
  const r=await api<Shipment>(API+(selected?'/'+selected.id:''),{method:selected?'PUT':'POST',body:JSON.stringify({...draft,note:draft.note||null,...(selected?{row_version:selected.row_version}:{request_key:createKey.current})})})
  attempts.current.delete(r.id);accept(r);setEditing(false);remember(r.id);await Promise.all([list(),references(),loadPositions(r.sales_order_line_id)]);setFeedback({notice:'銷售出貨草稿已儲存；實際成品庫存與來源未出貨量尚未變更。'})
 }catch(e){setFeedback({error:(e as Error).message})}finally{leave()}}
 async function remove(){if(!selected||!window.confirm('確定刪除銷售出貨草稿「'+selected.document_no+'」？已過帳或被引用的資料不能刪除。')||!enter())return;setFeedback({});try{const id=selected.id;await api(API+'/'+id+'?row_version='+selected.row_version,{method:'DELETE'});attempts.current.delete(id);fresh();await Promise.all([list(),references()]);setFeedback({notice:'銷售出貨草稿已刪除，訂單與實體庫存未變更。'})}catch(e){setFeedback({error:(e as Error).message})}finally{leave()}}
 async function inspect(){if(!selected||!enter())return;setFeedback({});setReviewFeedback({});try{
  const current=await api<Shipment>(API+'/'+selected.id);if(current.posting_state==='POSTED'){accept(current);setEditing(false);await Promise.all([list(),references()]);setFeedback({notice:'此出貨單已過帳，已重新讀取原始分錄；沒有再次扣除庫存。'});return}
  const pending=attempts.current.get(selected.id);if(pending){setReview(pending.review);setReviewFeedback({notice:'保留上次原請求以安全重試；也可取消後重新讀取，核對是否已過帳。'})}else{const result=await api<Review>(API+'/'+selected.id+'/posting-review');setReview(result);setPolicy(result.policy);accept(result.shipment)}
 }catch(e){setFeedback({error:(e as Error).message})}finally{leave()}}
 function closeReview(){if(lock.current)return;dialog.current?.close();setReview(null);setReviewFeedback({})}
 async function post(){if(!review?.confirmation_token||!review.policy.posting_allowed||!enter())return;setReviewFeedback({});const attempt=attempts.current.get(review.shipment.id)??{id:review.shipment.id,body:{request_key:crypto.randomUUID(),row_version:review.shipment.row_version,confirmation_token:review.confirmation_token},review};attempts.current.set(attempt.id,attempt)
  try{const r=await api<Shipment>(API+'/'+attempt.id+'/post',{method:'POST',body:JSON.stringify(attempt.body)});accept(r);setEditing(false);remember(r.id);dialog.current?.close();setReview(null);setFeedback({notice:'銷售出貨已實際過帳；已提交一筆負向成品庫存分錄。原銷售訂單金額與原料庫存未變更。'});try{await Promise.all([list(),references(),loadPositions(r.sales_order_line_id)])}catch(e){setFeedback({error:'出貨已過帳，但清單重新讀取失敗：'+(e as Error).message})}}
  catch(e){setReviewFeedback({error:(e as Error).message+' 尚未取得成功回應；可保留原請求重試，或取消後重新讀取核對實際狀態。'})}finally{leave()}}
 const actions=<div className="form-actions" data-feedback-group><ActionFeedback {...feedback} busy={busy} onReload={selected?()=>{attempts.current.delete(selected.id);void open(selected.id)}:undefined}/>
  {editing?<button className="button primary" type="submit" disabled={busy||!loaded}>{selected?'儲存變更':'儲存出貨草稿'}</button>:canEdit&&<button className="button primary" type="button" disabled={busy} onClick={()=>{const s=sources.find(s=>s.source_line_id===selected!.sales_order_line_id)??selected!.source;setDraft({...from(selected!),line_version:s.line_version,order_version:s.order_version});setWarehouse(selected!.warehouse_id);setEditing(true);setFeedback({})}}>編輯出貨單</button>}
  {canEdit&&<button className="button danger" type="button" disabled={busy} onClick={()=>void remove()}>刪除出貨草稿</button>}
  {editing&&<button className="button" type="button" disabled={busy} onClick={cancel}>取消</button>}
  {!editing&&canEdit&&<button className="button primary" type="button" disabled={busy||!policy?.posting_allowed} onClick={()=>void inspect()}>檢視銷售出貨過帳</button>}
  {selected&&<button className="button" type="button" disabled={busy} onClick={()=>{attempts.current.delete(selected.id);void open(selected.id)}}>重新讀取出貨單</button>}
 </div>
 return <>
  <div className="page-heading"><div><div className="eyebrow">SALES / SALES SHIPMENTS</div><h1>銷售出貨管理</h1><p>由實際銷售明細選取既有成品庫存位置，另行確認實體出貨。</p></div><button className="button primary new-button" type="button" disabled={busy||!loaded} onClick={fresh}>＋ 新增銷售出貨單</button></div>
  <div className="fg-receipt-notice" role="note"><strong>只有明確確認本張出貨單過帳，才會扣除成品實際庫存。</strong><p>{policy?.message??'本機展示模式／尚未啟用正式電子簽核與身分權限驗證'}</p><p>草稿、檢視、取消與 F5 都不會出貨。不變更銷售訂單金額或核准狀態，也不扣原料庫存。</p></div>
  <div className="po-layout"><section className="panel po-list" aria-label="銷售出貨單清單"><div className="panel-title"><h2>銷售出貨單清單 <span className="count">{rows.length}</span></h2></div>
   <div className="search-area" data-feedback-group><form className="search-bar" onSubmit={search}><label className="sr-only" htmlFor="ss-search">搜尋銷售出貨單</label><input id="ss-search" value={query} onChange={e=>setQuery(e.target.value)} placeholder="搜尋出貨單、訂單、客戶或成品"/><button type="submit">搜尋</button></form><ActionFeedback {...listFeedback}/></div>
   {rows.map(r=><div key={r.id} className={'po-list-row '+(selected?.id===r.id?'current-row':'')} data-feedback-group><button className="record-link" type="button" disabled={busy} aria-label={'開啟銷售出貨單 '+r.document_no} onClick={()=>void open(r.id)}><span>{r.document_no} · {r.posting_state==='POSTED'?'已過帳':'草稿'}</span><strong>{r.source.product_code} · {r.source.product_name}</strong><span>{r.source.sales_order_no} ／ {r.source.customer_code}</span><span>{r.quantity} {r.source.unit_code} · {r.business_date}</span></button><ActionFeedback error={rowError?.id===r.id?rowError.message:''}/></div>)}
   {!rows.length&&<p className="empty-state">{applied?'沒有符合的銷售出貨單。':'尚無銷售出貨單；銷售訂單不會自動建立出貨單。'}</p>}
  </section><div className="po-detail"><section className="panel" aria-label="銷售出貨單資料"><div className="panel-title"><h2>{selected?.document_no??'新增銷售出貨單'}</h2><span data-testid="ss-state">{posted?'POSTED · 不可變歷史':'DRAFT · 草稿／未出貨'}</span></div>
   {editing?<form aria-label="編輯銷售出貨單" onSubmit={save}><fieldset className="form-body po-fields fg-receipt-fields" disabled={busy||!loaded}>
    <div><label htmlFor="ss-number">銷售出貨單號 *</label><input id="ss-number" required value={draft.document_no} onChange={e=>change('document_no',e.target.value)}/></div>
    <div data-feedback-group><label htmlFor="ss-source">來源銷售明細 *</label><select id="ss-source" required value={draft.sales_order_line_id} onChange={e=>void chooseSource(e.target.value)}><option value="">請選擇實際銷售訂單明細</option>{sources.map(s=><option key={s.source_line_id} value={s.source_line_id}>{s.sales_order_no} · 明細 {s.line_no} · {s.customer_code} · {s.product_code} · 剩餘 {s.remaining_qty_base} {s.unit_code}</option>)}</select><ActionFeedback {...positionFeedback}/></div>
    {chosen&&<div className="po-wide"><SourceSummary source={chosen}/></div>}
    <div><label htmlFor="ss-date">出貨日期 *</label><input id="ss-date" type="date" required value={draft.business_date} onChange={e=>change('business_date',e.target.value)}/></div>
    <div><label htmlFor="ss-warehouse">出貨倉庫 *</label><select id="ss-warehouse" required value={warehouse} onChange={e=>chooseWarehouse(e.target.value)}><option value="">請明確選取實際出貨倉庫</option>{warehouses.map(w=><option key={w.warehouse_id} value={w.warehouse_id}>{w.warehouse_code} · {w.warehouse_name}</option>)}</select></div>
    <div><label htmlFor="ss-lot">出貨批號 *</label><select id="ss-lot" required value={draft.position_id} onChange={e=>change('position_id',e.target.value)}><option value="">請選擇此倉庫的既有庫存位置</option>{positions.filter(p=>p.warehouse_id===warehouse).map(p=><option key={p.id} value={p.id}>{p.lot_code??'無批次（依成品設定）'} · 庫存 {p.on_hand_qty_base}</option>)}</select></div>
    <div><label htmlFor="ss-current-stock">目前實際庫存</label><input id="ss-current-stock" data-testid="ss-edit-stock" readOnly value={chosenPosition?chosenPosition.on_hand_qty_base+' '+(chosen?unitOf(chosen):''):'請先選取實際出貨位置'}/></div>
    <div><label htmlFor="ss-quantity">本次出貨數量 *</label><input id="ss-quantity" type="text" inputMode="decimal" required value={draft.quantity} onChange={e=>change('quantity',e.target.value)}/></div>
    <div className="po-wide"><label htmlFor="ss-note">備註</label><textarea id="ss-note" value={draft.note} onChange={e=>change('note',e.target.value)}/></div>
    <p className="field-note po-wide">只使用既有庫存位置，不建立或自動分配倉庫／批號。實際出貨必須為正數，且同時受此明細剩餘訂購量與選定位置實際庫存限制。</p>
    {chosen&&!positions.length&&<p className="field-note po-wide">此成品尚無可選的實際庫存位置；保留原銷售需求，不會建立虛構批次或出貨。</p>}
   </fieldset>{actions}</form>:selected&&<><dl className="po-header-summary"><dt>銷售出貨單</dt><dd data-testid="ss-number-value">{selected.document_no}</dd><dt>來源銷售訂單</dt><dd><a href={'#sales-orders?record='+selected.sales_order_id}>{selected.source.sales_order_no} ↗</a></dd>
    <dt>客戶</dt><dd>{selected.source.customer_code} · {selected.source.customer_name}</dd><dt>出貨日期</dt><dd>{selected.business_date}</dd><dt>出貨倉庫</dt><dd>{selected.warehouse_code} · {selected.warehouse_name}</dd><dt>出貨批號</dt><dd data-testid="ss-lot-value">{selected.lot_code??'無批次（依成品設定）'}</dd>
    <dt>本次出貨數量</dt><dd data-testid="ss-quantity-value">{selected.quantity} {unitOf(selected.source)}</dd><dt>目前實際庫存</dt><dd data-testid="ss-current-stock">{selected.current_stock_qty_base} {unitOf(selected.source)}</dd><dt>備註</dt><dd>{selected.note||'—'}</dd>
    <dt>出貨單／來源明細識別</dt><dd className="fg-receipt-identity">{selected.id}<br/>{selected.sales_order_line_id}</dd><dt>過帳識別／時間</dt><dd className="fg-receipt-identity">{selected.posting_id??'未過帳'}<br/>{selected.posted_at??'—'}</dd>
    {posted&&<><dt>過帳性質</dt><dd>{selected.authorization_basis} · 本機展示人工確認；已過帳資料不可直接修改或刪除。</dd></>}
   </dl>{actions}</>}
  </section>
   {!editing&&selected&&<section className="panel"><SourceSummary source={sources.find(s=>s.source_line_id===selected.sales_order_line_id)??selected.source}/></section>}
   <section className="fg-receipt-refresh" data-feedback-group><button className="button" type="button" disabled={busy} onClick={()=>void reloadSources()}>重新讀取來源與庫存</button><ActionFeedback {...sourceFeedback}/></section>
   {posted&&selected&&<section className="panel" aria-label="實際銷售出貨分錄"><div className="panel-title"><h2>實際銷售出貨分錄</h2></div>{selected.movements.map(m=><dl className="po-header-summary fg-receipt-movement" key={m.id}><dt>實際庫存異動</dt><dd data-testid="ss-movement">{m.qty_base_delta} {unitOf(selected.source)}</dd><dt>成品／出貨倉庫</dt><dd>{selected.source.product_code} ／ {selected.warehouse_code}</dd><dt>出貨批號</dt><dd>{m.lot_code??'無批次（依成品設定）'}</dd><dt>分錄／庫存位置識別</dt><dd className="fg-receipt-identity">{m.id}<br/>{m.position_id}</dd></dl>)}</section>}
  </div></div>
  <dialog ref={dialog} className="fg-receipt-dialog" aria-labelledby="ss-review-title" onCancel={e=>{e.preventDefault();closeReview()}}>{review&&<><h2 id="ss-review-title">檢視銷售出貨過帳</h2><p>{review.policy.message}</p><p className="fg-receipt-notice">確認後將寫入一筆負向成品庫存分錄；原訂單金額、完工回報、成品入庫與原料庫存不變。</p><SourceSummary source={review.source} prefix="ss-review-source"/>
   <dl className="po-header-summary"><dt>銷售出貨單</dt><dd>{review.shipment.document_no}</dd><dt>出貨日期</dt><dd>{review.shipment.business_date}</dd><dt>出貨倉庫</dt><dd>{review.shipment.warehouse_code} · {review.shipment.warehouse_name}</dd><dt>出貨批號</dt><dd>{review.shipment.lot_code??'無批次（依成品設定）'}</dd><dt>本次出貨數量</dt><dd>{review.shipment.quantity} {unitOf(review.source)}</dd>
    <dt>目前實際庫存</dt><dd data-testid="ss-review-current-stock">{review.current_stock_qty_base} {unitOf(review.source)}</dd><dt>預計出貨後庫存</dt><dd data-testid="ss-expected-stock">{review.expected_stock_qty_base} {unitOf(review.source)}</dd><dt>出貨後剩餘未出貨量</dt><dd data-testid="ss-review-remaining-after">{review.remaining_after_qty_base} {unitOf(review.source)}</dd><dt>備註</dt><dd>{review.shipment.note||'—'}</dd></dl>
   <div className="form-actions" data-feedback-group><ActionFeedback {...reviewFeedback}/><button className="button primary" type="button" disabled={busy||!review.policy.posting_allowed||!review.confirmation_token} onClick={()=>void post()}>確認出貨並扣除成品庫存</button><button ref={reviewCancel} className="button" type="button" disabled={busy} onClick={closeReview}>取消</button></div>
  </>}</dialog>
 </>
}
