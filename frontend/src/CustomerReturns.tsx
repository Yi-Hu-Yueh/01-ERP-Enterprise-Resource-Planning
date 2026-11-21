import {useEffect,useRef,useState,type FormEvent} from 'react'
import {api} from './api'
import {ActionFeedback} from './ActionFeedback'
import './FinishedGoodsReceiving.css'

type Source={shipment_id:string;shipment_version:number;shipment_no:string;sales_order_line_id:string;sales_order_id:string;sales_order_no:string;
 customer_id:string;customer_code:string;customer_name:string;product_id:string;product_code:string;product_name:string;
 unit_id:string;unit_code:string;unit_name:string;position_id:string;warehouse_id:string;warehouse_code:string;warehouse_name:string;
 lot_id:string|null;lot_code:string|null;original_shipped_qty_base:string;finalized_returned_qty_base:string;remaining_returnable_qty_base:string;current_stock_qty_base:string}
type Policy={posting_allowed:boolean;authorization_basis:string|null;message:string}
type Draft={document_no:string;shipment_id:string;shipment_version:number;business_date:string;quantity:string;restock_eligible:''|'true'|'false';reason:string;note:string}
type Movement={id:string;finalization_id:string;return_id:string;position_id:string;qty_base_delta:string;item_id:string;warehouse_id:string;lot_id:string|null;lot_code:string|null}
type ReturnDocument=Omit<Draft,'reason'|'note'|'restock_eligible'>&{id:string;row_version:number;restock_eligible:boolean;reason:string|null;note:string|null;
 sales_order_line_id:string;sales_order_id:string;customer_id:string;product_id:string;unit_id:string;position_id:string;warehouse_id:string;lot_id:string|null;
 source:Source;finalization_state:string;finalization_id:string|null;finalized_at:string|null;authorization_basis:string|null;current_stock_qty_base:string;movements:Movement[]}
type Review={return_document:ReturnDocument;source:Source;current_stock_qty_base:string;expected_stock_qty_base:string;remaining_after_qty_base:string;confirmation_token:string|null;policy:Policy}
type FinalizeBody={request_key:string;row_version:number;confirmation_token:string}
type Attempt={id:string;body:FinalizeBody;review:Review}
type Feedback={error?:string;notice?:string}
const API='/customer-returns'
const blank=():Draft=>({document_no:'',shipment_id:'',shipment_version:0,business_date:'',quantity:'',restock_eligible:'',reason:'',note:''})
const from=(r:ReturnDocument):Draft=>({document_no:r.document_no,shipment_id:r.shipment_id,shipment_version:r.shipment_version,
 business_date:r.business_date,quantity:r.quantity,restock_eligible:r.restock_eligible?'true':'false',reason:r.reason??'',note:r.note??''})
const remember=(id?:string)=>history.replaceState(null,'','#customer-returns'+(id?'?record='+encodeURIComponent(id):''))
const unitOf=(s:Source)=>s.unit_code+' · '+s.unit_name
const completed=(r:ReturnDocument)=>r.finalization_state==='POSTED'||r.finalization_state==='RECORDED_NO_RESTOCK'
const stateLabel=(r:ReturnDocument)=>r.finalization_state==='POSTED'?'POSTED · 已回庫':r.finalization_state==='RECORDED_NO_RESTOCK'?'RECORDED_NO_RESTOCK · 已登記／不回庫':'DRAFT · 草稿／未完成'

function SourceSummary({source,prefix='cr-source'}:{source:Source;prefix?:string}){
 const unit=unitOf(source)
 return <section className="fg-receipt-source" aria-label={prefix==='cr-source'?'原出貨來源與可退貨量':'退貨確認來源與可退貨量'}>
  <h3>原出貨來源 · {source.shipment_no}</h3><dl className="po-header-summary">
   <dt>來源銷售出貨單</dt><dd><a href={'#sales-shipments?record='+source.shipment_id}>{source.shipment_no} ↗</a></dd>
   <dt>原始銷售訂單</dt><dd><a href={'#sales-orders?record='+source.sales_order_id}>{source.sales_order_no} ↗</a></dd>
   <dt>客戶</dt><dd data-testid={prefix+'-customer'}>{source.customer_code} · {source.customer_name}</dd>
   <dt>成品／基本單位</dt><dd>{source.product_code} · {source.product_name} ／ {unit}</dd>
   <dt>原出貨倉庫</dt><dd>{source.warehouse_code} · {source.warehouse_name}</dd>
   <dt>原出貨批號</dt><dd>{source.lot_code??'無批次（依成品設定）'}</dd>
   <dt>原出貨數量</dt><dd data-testid={prefix+'-original'}>{source.original_shipped_qty_base} {unit}</dd>
   <dt>累計已退貨</dt><dd data-testid={prefix+'-returned'}>{source.finalized_returned_qty_base} {unit}</dd>
   <dt>剩餘可退貨量</dt><dd data-testid={prefix+'-remaining'}>{source.remaining_returnable_qty_base} {unit}</dd>
  </dl><p className="field-note">已完成的回庫與不回庫退貨都計入可退貨量；草稿不占用。原始累計出貨與訂單剩餘未出貨量不會因退貨減少或重新增加。</p>
 </section>
}

export function CustomerReturns({onConnection}:{onConnection:(online:boolean)=>void}){
 const [rows,setRows]=useState<ReturnDocument[]>([]),[sources,setSources]=useState<Source[]>([])
 const [selected,setSelected]=useState<ReturnDocument|null>(null),[draft,setDraft]=useState<Draft>(blank),[editing,setEditing]=useState(true)
 const [query,setQuery]=useState(''),[applied,setApplied]=useState(''),[busy,setBusy]=useState(false),[loaded,setLoaded]=useState(false),[policy,setPolicy]=useState<Policy|null>(null)
 const [feedback,setFeedback]=useState<Feedback>({}),[listFeedback,setListFeedback]=useState<Feedback>({}),[sourceFeedback,setSourceFeedback]=useState<Feedback>({})
 const [rowError,setRowError]=useState<{id:string;message:string}|null>(null),[review,setReview]=useState<Review|null>(null),[reviewFeedback,setReviewFeedback]=useState<Feedback>({})
 const lock=useRef(false),createKey=useRef(crypto.randomUUID()),appliedQuery=useRef(''),attempts=useRef(new Map<string,Attempt>())
 const dialog=useRef<HTMLDialogElement>(null),reviewCancel=useRef<HTMLButtonElement>(null)
 const chosen=sources.find(s=>s.shipment_id===draft.shipment_id)??(selected?.shipment_id===draft.shipment_id?selected.source:undefined)
 const canEdit=selected?.finalization_state==='DRAFT'&&!selected.finalization_id,finalized=selected?completed(selected):false
 const enter=()=>{if(lock.current)return false;lock.current=true;setBusy(true);return true}
 const leave=()=>{lock.current=false;setBusy(false)}
 async function list(q=appliedQuery.current){setRows(await api<ReturnDocument[]>(API+'?q='+encodeURIComponent(q)));setListFeedback({});onConnection(true)}
 async function references(){const [s,p]=await Promise.all([api<Source[]>(API+'/sources'),api<Policy>(API+'/policy')]);setSources(s);setPolicy(p);setLoaded(true);onConnection(true)}
 function accept(r:ReturnDocument){setSelected(r);setDraft(from(r));if(completed(r))attempts.current.delete(r.id)}
 function fresh(){setSelected(null);setDraft(blank());setEditing(true);setFeedback({});setSourceFeedback({});setRowError(null);createKey.current=crypto.randomUUID();remember()}
 async function open(id:string){if(!enter())return;setFeedback({});setSourceFeedback({});setRowError(null)
  try{const r=await api<ReturnDocument>(API+'/'+id);accept(r);setEditing(false);remember(r.id);await references()}
  catch(e){const message=(e as Error).message;setRowError({id,message});setListFeedback({error:message})}finally{leave()}}
 useEffect(()=>{
  Promise.all([list(''),references()]).then(async()=>{const id=new URLSearchParams(location.hash.split('?')[1]||'').get('record');if(id)await open(id)}).catch(e=>{setListFeedback({error:e.message});onConnection(false)})
  const sync=()=>{if(location.hash.slice(1).split('?')[0]!=='customer-returns')return;const id=new URLSearchParams(location.hash.split('?')[1]||'').get('record');if(id)void open(id);else fresh()}
  window.addEventListener('hashchange',sync);return()=>window.removeEventListener('hashchange',sync)
 },[])
 useEffect(()=>{if(review&&!dialog.current?.open){dialog.current?.showModal();reviewCancel.current?.focus()}},[review])
 function change(key:Exclude<keyof Draft,'restock_eligible'>,value:string){setDraft(d=>({...d,[key]:value}))}
 function chooseSource(id:string){const source=sources.find(s=>s.shipment_id===id);setSourceFeedback({});setDraft(d=>({...d,shipment_id:id,shipment_version:source?.shipment_version??0}))}
 function cancel(){if(selected){setDraft(from(selected));setEditing(false);setFeedback({});setSourceFeedback({})}else fresh()}
 useEffect(()=>{const escape=(e:KeyboardEvent)=>{if(e.key==='Escape'&&!lock.current&&!dialog.current?.open&&editing)cancel()};window.addEventListener('keydown',escape);return()=>window.removeEventListener('keydown',escape)},[editing,selected])
 async function search(e:FormEvent){e.preventDefault();setApplied(query);appliedQuery.current=query;try{await list(query)}catch(e){setListFeedback({error:(e as Error).message})}}
 async function reloadSources(){if(!enter())return;setSourceFeedback({});try{await references();if(selected&&!editing)accept(await api<ReturnDocument>(API+'/'+selected.id));setSourceFeedback({notice:'已重新讀取來源、可退貨量與實際庫存；未送出異動請求，也未代為完成退貨。'})}catch(e){setSourceFeedback({error:(e as Error).message})}finally{leave()}}
 async function save(e:FormEvent){e.preventDefault();if(!enter())return;setFeedback({});try{
  if(!chosen)throw Error('請選擇真實已過帳的來源銷售出貨單。')
  if(draft.restock_eligible==='')throw Error('請明確選擇本次退貨是否確認可回庫。')
  const r=await api<ReturnDocument>(API+(selected?'/'+selected.id:''),{method:selected?'PUT':'POST',body:JSON.stringify({...draft,restock_eligible:draft.restock_eligible==='true',reason:draft.reason||null,note:draft.note||null,...(selected?{row_version:selected.row_version}:{request_key:createKey.current})})})
  attempts.current.delete(r.id);accept(r);setEditing(false);remember(r.id);await Promise.all([list(),references()]);setFeedback({notice:'客戶退貨草稿已儲存；實際庫存與累計已退貨量尚未變更，還需另行檢視並明確確認。'})
 }catch(e){setFeedback({error:(e as Error).message})}finally{leave()}}
 async function remove(){if(!selected||!window.confirm('確定刪除客戶退貨草稿「'+selected.document_no+'」？已完成或被引用的退貨不能刪除。')||!enter())return;setFeedback({});try{const id=selected.id;await api(API+'/'+id+'?row_version='+selected.row_version,{method:'DELETE'});attempts.current.delete(id);fresh();await Promise.all([list(),references()]);setFeedback({notice:'客戶退貨草稿已刪除；出貨、訂單與實體庫存未變更。'})}catch(e){setFeedback({error:(e as Error).message})}finally{leave()}}
 async function inspect(){if(!selected||!enter())return;setFeedback({});setReviewFeedback({});try{
  const current=await api<ReturnDocument>(API+'/'+selected.id);if(completed(current)){accept(current);setEditing(false);await Promise.all([list(),references()]);setFeedback({notice:'此退貨已完成，已讀取原始結果；沒有再次登記或增加庫存。'});return}
  const pending=attempts.current.get(selected.id);if(pending){setReview(pending.review);setReviewFeedback({notice:'保留上次原請求以安全重試；也可取消後重新讀取，核對是否已完成。'})}else{const result=await api<Review>(API+'/'+selected.id+'/finalization-review');setReview(result);setPolicy(result.policy);accept(result.return_document)}
 }catch(e){setFeedback({error:(e as Error).message})}finally{leave()}}
 function closeReview(){if(lock.current)return;dialog.current?.close();setReview(null);setReviewFeedback({})}
 async function finalize(){if(!review?.confirmation_token||!review.policy.posting_allowed||!enter())return;setReviewFeedback({});const attempt=attempts.current.get(review.return_document.id)??{id:review.return_document.id,body:{request_key:crypto.randomUUID(),row_version:review.return_document.row_version,confirmation_token:review.confirmation_token},review};attempts.current.set(attempt.id,attempt)
  try{const r=await api<ReturnDocument>(API+'/'+attempt.id+'/finalize',{method:'POST',body:JSON.stringify(attempt.body)});accept(r);setEditing(false);remember(r.id);dialog.current?.close();setReview(null);setFeedback({notice:r.finalization_state==='POSTED'?'客戶退貨已回庫，已提交一筆正向成品庫存分錄；原始出貨、訂單金額與原料庫存不變。':'客戶退貨已完成登記（不回庫）；已計入累計退貨量，未建立任何庫存分錄或增加可用庫存。'});try{await Promise.all([list(),references()])}catch(e){setFeedback({error:'退貨已完成，但清單重新讀取失敗：'+(e as Error).message})}}
  catch(e){setReviewFeedback({error:(e as Error).message+' 尚未取得成功回應；可保留原請求重試，或取消後重新讀取核對實際狀態。'})}finally{leave()}}
 const actions=<div className="form-actions" data-feedback-group><ActionFeedback {...feedback} busy={busy} onReload={selected?()=>{attempts.current.delete(selected.id);void open(selected.id)}:undefined}/>
  {editing?<button className="button primary" type="submit" disabled={busy||!loaded}>{selected?'儲存變更':'儲存退貨草稿'}</button>:canEdit&&<button className="button primary" type="button" disabled={busy} onClick={()=>{const s=sources.find(s=>s.shipment_id===selected!.shipment_id)??selected!.source;setDraft({...from(selected!),shipment_version:s.shipment_version});setEditing(true);setFeedback({})}}>編輯退貨單</button>}
  {canEdit&&<button className="button danger" type="button" disabled={busy} onClick={()=>void remove()}>刪除退貨草稿</button>}
  {editing&&<button className="button" type="button" disabled={busy} onClick={cancel}>取消</button>}
  {!editing&&canEdit&&<button className="button primary" type="button" disabled={busy||!policy?.posting_allowed} onClick={()=>void inspect()}>檢視客戶退貨確認</button>}
  {selected&&<button className="button" type="button" disabled={busy} onClick={()=>{attempts.current.delete(selected.id);void open(selected.id)}}>重新讀取退貨單</button>}
 </div>
 return <>
  <div className="page-heading"><div><div className="eyebrow">SALES / CUSTOMER RETURNS</div><h1>客戶退貨管理</h1><p>依真實已過帳出貨登記退貨；是否回庫必須另行明確指定與確認。</p></div><button className="button primary new-button" type="button" disabled={busy||!loaded} onClick={fresh}>＋ 新增客戶退貨單</button></div>
  <div className="fg-receipt-notice" role="note"><strong>登記退貨不等於回庫；只有確認可回庫並明確完成回庫，才會增加成品實際庫存。</strong><p>{policy?.message??'本機展示模式／尚未啟用正式電子簽核與身分權限驗證'}</p><p>草稿、檢視、取消與 F5 都不會完成退貨。回庫僅使用原出貨倉庫與批號；人工指定不代表正式品質核准。</p></div>
  <div className="po-layout"><section className="panel po-list" aria-label="客戶退貨單清單"><div className="panel-title"><h2>客戶退貨單清單 <span className="count">{rows.length}</span></h2></div>
   <div className="search-area" data-feedback-group><form className="search-bar" onSubmit={search}><label className="sr-only" htmlFor="cr-search">搜尋客戶退貨單</label><input id="cr-search" value={query} onChange={e=>setQuery(e.target.value)} placeholder="搜尋退貨單、出貨單、訂單、客戶或成品"/><button type="submit">搜尋</button></form><ActionFeedback {...listFeedback}/></div>
   {rows.map(r=><div key={r.id} className={'po-list-row '+(selected?.id===r.id?'current-row':'')} data-feedback-group><button className="record-link" type="button" disabled={busy} aria-label={'開啟客戶退貨單 '+r.document_no} onClick={()=>void open(r.id)}><span>{r.document_no} · {stateLabel(r)}</span><strong>{r.source.product_code} · {r.source.product_name}</strong><span>{r.source.shipment_no} ／ {r.source.customer_code}</span><span>{r.quantity} {r.source.unit_code} · {r.business_date}</span></button><ActionFeedback error={rowError?.id===r.id?rowError.message:''}/></div>)}
   {!rows.length&&<p className="empty-state">{applied?'沒有符合的客戶退貨單。':'尚無客戶退貨單；銷售出貨不會自動建立退貨。'}</p>}
  </section><div className="po-detail"><section className="panel" aria-label="客戶退貨單資料"><div className="panel-title"><h2>{selected?.document_no??'新增客戶退貨單'}</h2><span data-testid="cr-state">{selected?stateLabel(selected):'DRAFT · 草稿／未完成'}</span></div>
   {editing?<form aria-label="編輯客戶退貨單" onSubmit={save}><fieldset className="form-body po-fields fg-receipt-fields" disabled={busy||!loaded}>
    <div><label htmlFor="cr-number">客戶退貨單號 *</label><input id="cr-number" required value={draft.document_no} onChange={e=>change('document_no',e.target.value)}/></div>
    <div><label htmlFor="cr-source">來源銷售出貨單 *</label><select id="cr-source" required value={draft.shipment_id} onChange={e=>chooseSource(e.target.value)}><option value="">請選擇真實已過帳的銷售出貨單</option>{sources.map(s=><option key={s.shipment_id} value={s.shipment_id}>{s.shipment_no} · {s.customer_code} · {s.product_code} · 可退 {s.remaining_returnable_qty_base} {s.unit_code}</option>)}</select></div>
    {chosen&&<div className="po-wide"><SourceSummary source={chosen}/></div>}
    <div><label htmlFor="cr-date">退貨日期 *</label><input id="cr-date" type="date" required value={draft.business_date} onChange={e=>change('business_date',e.target.value)}/></div>
    <div><label htmlFor="cr-quantity">本次退貨數量 *</label><input id="cr-quantity" type="text" inputMode="decimal" required value={draft.quantity} onChange={e=>change('quantity',e.target.value)}/></div>
    <div><label htmlFor="cr-restock">是否確認可回庫 *</label><select id="cr-restock" required value={draft.restock_eligible} onChange={e=>setDraft(d=>({...d,restock_eligible:e.target.value as Draft['restock_eligible']}))}><option value="">請明確選擇本次處理方式</option><option value="false">不回庫：只登記退貨，不增加庫存</option><option value="true">確認可回庫：完成時增加原位置庫存</option></select></div>
    <div><label htmlFor="cr-stock">目前實際庫存</label><input id="cr-stock" data-testid="cr-edit-stock" readOnly value={chosen?chosen.current_stock_qty_base+' '+unitOf(chosen):'請先選取原出貨來源'}/></div>
    <div className="po-wide"><label htmlFor="cr-reason">退貨原因</label><textarea id="cr-reason" value={draft.reason} onChange={e=>change('reason',e.target.value)}/></div>
    <div className="po-wide"><label htmlFor="cr-note">備註</label><textarea id="cr-note" value={draft.note} onChange={e=>change('note',e.target.value)}/></div>
    <p className="field-note po-wide">已完成退貨數量不得超過這張來源出貨的可退量。不回庫也會占用可退量；回庫只恢復原位置成品，不建立新批號、退款或品質檢驗流程。</p>
   </fieldset>{actions}</form>:selected&&<><dl className="po-header-summary"><dt>客戶退貨單</dt><dd data-testid="cr-number-value">{selected.document_no}</dd><dt>來源銷售出貨單</dt><dd><a href={'#sales-shipments?record='+selected.shipment_id}>{selected.source.shipment_no} ↗</a></dd>
    <dt>退貨日期</dt><dd>{selected.business_date}</dd><dt>本次退貨數量</dt><dd data-testid="cr-quantity-value">{selected.quantity} {unitOf(selected.source)}</dd><dt>本次處理方式</dt><dd data-testid="cr-restock-value">{selected.restock_eligible?'確認可回庫':'不回庫：只登記退貨'}</dd>
    <dt>目前實際庫存</dt><dd data-testid="cr-current-stock">{selected.current_stock_qty_base} {unitOf(selected.source)}</dd><dt>退貨原因</dt><dd>{selected.reason||'—'}</dd><dt>備註</dt><dd>{selected.note||'—'}</dd>
    <dt>退貨單／來源出貨識別</dt><dd className="fg-receipt-identity">{selected.id}<br/>{selected.shipment_id}</dd><dt>完成識別／時間</dt><dd className="fg-receipt-identity">{selected.finalization_id??'未完成'}<br/>{selected.finalized_at??'—'}</dd>
    {finalized&&<><dt>完成性質</dt><dd>{selected.authorization_basis} · 本機展示人工確認；已完成資料不可直接修改或刪除。</dd></>}
   </dl>{actions}</>}
  </section>
   {!editing&&selected&&<section className="panel"><SourceSummary source={sources.find(s=>s.shipment_id===selected.shipment_id)??selected.source}/></section>}
   <section className="fg-receipt-refresh" data-feedback-group><button className="button" type="button" disabled={busy} onClick={()=>void reloadSources()}>重新讀取來源與庫存</button><ActionFeedback {...sourceFeedback}/></section>
   {finalized&&selected&&<section className="panel" aria-label="客戶退貨實際庫存結果"><div className="panel-title"><h2>客戶退貨實際庫存結果</h2></div>{selected.finalization_state==='RECORDED_NO_RESTOCK'?<p className="empty-state" data-testid="cr-no-movement">已完成退貨登記（不回庫）；零庫存分錄，未增加可用成品庫存。</p>:selected.movements.map(m=><dl className="po-header-summary fg-receipt-movement" key={m.id}><dt>實際庫存異動</dt><dd data-testid="cr-movement">+{m.qty_base_delta} {unitOf(selected.source)}</dd><dt>成品／原出貨倉庫</dt><dd>{selected.source.product_code} ／ {selected.source.warehouse_code}</dd><dt>原出貨批號</dt><dd>{m.lot_code??'無批次（依成品設定）'}</dd><dt>分錄／庫存位置識別</dt><dd className="fg-receipt-identity">{m.id}<br/>{m.position_id}</dd></dl>)}</section>}
  </div></div>
  <dialog ref={dialog} className="fg-receipt-dialog" aria-labelledby="cr-review-title" onCancel={e=>{e.preventDefault();closeReview()}}>{review&&<><h2 id="cr-review-title">檢視客戶退貨確認</h2><p>{review.policy.message}</p><p className="fg-receipt-notice">{review.return_document.restock_eligible?'本次已明確指定可回庫；確認後將在原出貨位置寫入一筆正向成品庫存分錄。':'本次指定不回庫；確認僅登記已完成退貨並占用可退量，不會建立庫存分錄。'} 原始出貨、訂單金額、完工回報、成品入庫與原料庫存不變。</p><SourceSummary source={review.source} prefix="cr-review-source"/>
   <dl className="po-header-summary"><dt>客戶退貨單</dt><dd>{review.return_document.document_no}</dd><dt>退貨日期</dt><dd>{review.return_document.business_date}</dd><dt>本次退貨數量</dt><dd>{review.return_document.quantity} {unitOf(review.source)}</dd><dt>本次處理方式</dt><dd>{review.return_document.restock_eligible?'確認可回庫':'不回庫：只登記退貨'}</dd>
    <dt>目前實際庫存</dt><dd data-testid="cr-review-current-stock">{review.current_stock_qty_base} {unitOf(review.source)}</dd><dt>預計退貨後庫存</dt><dd data-testid="cr-expected-stock">{review.expected_stock_qty_base} {unitOf(review.source)}</dd><dt>完成後剩餘可退貨量</dt><dd data-testid="cr-review-remaining-after">{review.remaining_after_qty_base} {unitOf(review.source)}</dd><dt>退貨原因／備註</dt><dd>{review.return_document.reason||'—'} ／ {review.return_document.note||'—'}</dd></dl>
   <div className="form-actions" data-feedback-group><ActionFeedback {...reviewFeedback}/><button className="button primary" type="button" disabled={busy||!review.policy.posting_allowed||!review.confirmation_token} onClick={()=>void finalize()}>{review.return_document.restock_eligible?'確認退貨並增加成品庫存':'確認登記退貨（不回庫）'}</button><button ref={reviewCancel} className="button" type="button" disabled={busy} onClick={closeReview}>取消</button></div>
  </>}</dialog>
 </>
}
