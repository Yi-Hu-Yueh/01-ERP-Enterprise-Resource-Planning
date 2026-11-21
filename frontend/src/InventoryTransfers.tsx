import {useEffect,useRef,useState,type FormEvent} from 'react'
import {api} from './api'
import {ActionFeedback} from './ActionFeedback'
import './FinishedGoodsReceiving.css'

type Position={position_id:string|null;item_id:string;item_kind:string;item_code:string;item_name:string;unit_id:string;unit_code:string;unit_name:string;warehouse_id:string;warehouse_code:string;warehouse_name:string;lot_id:string|null;lot_code:string|null;current_stock_qty_base:string;frozen:boolean;active_stocktake_no:string|null;will_create?:boolean;lot_origin_kind?:string;supplier_lot_code?:string|null}
type Policy={posting_allowed:boolean;authorization_basis:string|null;message:string}
type Draft={document_no:string;business_date:string;item_id:string;source_warehouse_id:string;source_position_id:string;destination_warehouse_id:string;quantity:string;reason:string;note:string}
type Movement={id:string;posting_id:string;transfer_id:string;position_id:string;leg_no:number;qty_base_delta:string}
type Balances={source_before_qty_base:string;destination_before_qty_base:string;source_delta_qty_base:string;destination_delta_qty_base:string;source_after_qty_base:string;destination_after_qty_base:string;net_qty_base:string}
type Transfer=Omit<Draft,'note'>&Balances&{id:string;row_version:number;state:'DRAFT'|'POSTED'|'CANCELLED';note:string|null;source:Position;destination:Position;blockers:string[];posting_id:string|null;posted_at:string|null;cancelled_at:string|null;destination_position_id:string|null;movements:Movement[]}
type Review=Policy&Balances&{transfer:Transfer;source:Position;destination:Position;confirmation_token:string|null}
type Attempt={id:string;body:{request_key:string;row_version:number;confirmation_token:string};review:Review}
type Feedback={error?:string;notice?:string}
const API='/inventory-transfers'
const blank=():Draft=>({document_no:'',business_date:'',item_id:'',source_warehouse_id:'',source_position_id:'',destination_warehouse_id:'',quantity:'',reason:'',note:''})
const from=(r:Transfer):Draft=>({document_no:r.document_no,business_date:r.business_date,item_id:r.source.item_id,source_warehouse_id:r.source.warehouse_id,source_position_id:r.source_position_id,destination_warehouse_id:r.destination_warehouse_id,quantity:r.quantity,reason:r.reason,note:r.note??''})
const remember=(id?:string,entry?:string)=>history.replaceState(null,'','#inventory-transfers'+(id?'?record='+encodeURIComponent(id)+(entry?'&entry='+encodeURIComponent(entry):''):''))
const unit=(s:Position)=>s.unit_code+' · '+s.unit_name
const signed=(s:string)=>s!=='0'&&!s.startsWith('-')?'+'+s:s
const stateName=(s:string)=>s==='DRAFT'?'DRAFT · 草稿':s==='POSTED'?'POSTED · 已過帳':'CANCELLED · 已取消'

function PositionSummary({position,side}:{position:Position;side:'source'|'destination'}){
 const title=side==='source'?'來源':'目的'
 return <section className="fg-receipt-source" aria-label={title+'庫存位置'}>
  <h3>{title}庫存位置</h3><dl className="po-header-summary">
   <dt>調撥品項</dt><dd>{position.item_code} · {position.item_name}</dd>
   <dt>原料／成品</dt><dd>{position.item_kind==='RAW'?'原料':'成品'}</dd>
   <dt>{title}倉庫</dt><dd data-testid={'trf-'+side+'-warehouse'}>{position.warehouse_code} · {position.warehouse_name}</dd>
   <dt>{title}批號</dt><dd data-testid={'trf-'+side+'-lot'}>{position.lot_code??'無批次（依品項設定）'}</dd>
   {position.lot_id&&position.lot_origin_kind&&<><dt>原批次來源</dt><dd>{position.lot_origin_kind==='MANUFACTURED'?'原製造批次':position.lot_origin_kind==='PURCHASED'?'原採購批次':position.lot_origin_kind}{position.supplier_lot_code?' ／ 原供應商批號 '+position.supplier_lot_code:''}</dd></>}
   <dt>基本單位</dt><dd>{unit(position)}</dd>
   <dt>{title}倉目前庫存</dt><dd data-testid={'trf-'+side+'-stock'}>{position.current_stock_qty_base} {unit(position)}</dd>
   <dt>{title}倉凍結狀態</dt><dd data-testid={'trf-'+side+'-freeze'}>{position.frozen?'倉庫已凍結：'+(position.active_stocktake_no??'進行中的盤點'):'未凍結'}</dd>
   {side==='destination'&&<><dt>目的位置處理</dt><dd data-testid="trf-destination-handling">{position.will_create?'尚無相同原批次位置；僅在最後確認成功的原子交易內建立。':'沿用此倉相同品項、單位與原批次的既有位置。'}</dd></>}
  </dl>
 </section>
}

function BalanceSummary({value,source,testPrefix}:{value:Balances;source:Position;testPrefix:string}){
 return <dl className="po-header-summary">
  <dt>來源倉過帳前庫存</dt><dd data-testid={testPrefix+'-source-before'}>{value.source_before_qty_base} {unit(source)}</dd>
  <dt>目的倉過帳前庫存</dt><dd data-testid={testPrefix+'-destination-before'}>{value.destination_before_qty_base} {unit(source)}</dd>
  <dt>來源倉扣庫</dt><dd data-testid={testPrefix+'-source-delta'}>{signed(value.source_delta_qty_base)} {unit(source)}</dd>
  <dt>目的倉入庫</dt><dd data-testid={testPrefix+'-destination-delta'}>{signed(value.destination_delta_qty_base)} {unit(source)}</dd>
  <dt>來源倉{testPrefix==='trf-review'?'預計':'過帳後'}庫存</dt><dd data-testid={testPrefix+'-source-after'}>{value.source_after_qty_base} {unit(source)}</dd>
  <dt>目的倉{testPrefix==='trf-review'?'預計':'過帳後'}庫存</dt><dd data-testid={testPrefix+'-destination-after'}>{value.destination_after_qty_base} {unit(source)}</dd>
  <dt>公司同品項／同單位淨庫存變動</dt><dd data-testid={testPrefix+'-net'}><strong>{value.net_qty_base} {unit(source)}（總量不變）</strong></dd>
 </dl>
}

export function InventoryTransfers({onConnection}:{onConnection:(online:boolean)=>void}){
 const [rows,setRows]=useState<Transfer[]>([]),[sources,setSources]=useState<Position[]>([]),[destinations,setDestinations]=useState<Position[]>([]),[policy,setPolicy]=useState<Policy|null>(null)
 const [selected,setSelected]=useState<Transfer|null>(null),[draft,setDraft]=useState<Draft>(blank),[editing,setEditing]=useState(true),[loaded,setLoaded]=useState(false),[busy,setBusy]=useState(false)
 const [query,setQuery]=useState(''),[applied,setApplied]=useState(''),[feedback,setFeedback]=useState<Feedback>({}),[listFeedback,setListFeedback]=useState<Feedback>({}),[sourceFeedback,setSourceFeedback]=useState<Feedback>({})
 const [review,setReview]=useState<Review|null>(null),[reviewFeedback,setReviewFeedback]=useState<Feedback>({})
 const lock=useRef(false),pendingOpen=useRef<string|null>(null),createKey=useRef(crypto.randomUUID()),appliedQuery=useRef(''),attempts=useRef(new Map<string,Attempt>()),cancelAttempts=useRef(new Map<string,{request_key:string;row_version:number}>())
 const dialog=useRef<HTMLDialogElement>(null),reviewCancel=useRef<HTMLButtonElement>(null)
 const chosen=sources.find(s=>s.position_id===draft.source_position_id)??(selected?.source_position_id===draft.source_position_id?selected.source:undefined)
 const destination=destinations.find(s=>s.warehouse_id===draft.destination_warehouse_id)
 const items=[...new Map(sources.map(s=>[s.item_id,s])).values()]
 const warehouses=[...new Map(sources.filter(s=>s.item_id===draft.item_id).map(s=>[s.warehouse_id,s])).values()]
 const positions=sources.filter(s=>s.item_id===draft.item_id&&s.warehouse_id===draft.source_warehouse_id)
 const canEdit=selected?.state==='DRAFT'
 const enter=()=>{if(lock.current)return false;lock.current=true;setBusy(true);return true}
 const leave=()=>{lock.current=false;setBusy(false);const next=pendingOpen.current;pendingOpen.current=null;if(next!==null){if(next)void open(next);else fresh()}}
 async function list(q=appliedQuery.current){setRows(await api<Transfer[]>(API+'?q='+encodeURIComponent(q)));setListFeedback({});onConnection(true)}
 async function references(){const [s,p]=await Promise.all([api<Position[]>(API+'/sources'),api<Policy>(API+'/policy')]);setSources(s);setPolicy(p);setLoaded(true);onConnection(true)}
 function accept(r:Transfer){setSelected(r);setDraft(from(r));if(r.state!=='DRAFT')attempts.current.delete(r.id)}
 function fresh(){setSelected(null);setDraft(blank());setDestinations([]);setEditing(true);setFeedback({});setSourceFeedback({});createKey.current=crypto.randomUUID();remember()}
 async function open(id:string){
  if(!enter()){pendingOpen.current=id;return}setFeedback({});setSourceFeedback({})
  try{const row=await api<Transfer>(API+'/'+id),route=new URLSearchParams(location.hash.split('?')[1]||''),entry=route.get('record')===id?route.get('entry'):null
   const target=entry&&row.movements.some(m=>m.id===entry)?entry:undefined
   accept(row);setEditing(false);remember(id,target);await references();if(target)document.getElementById('trf-entry-'+target)?.scrollIntoView({block:'center'})
  }catch(e){setListFeedback({error:(e as Error).message})}finally{leave()}
 }
 useEffect(()=>{
  Promise.all([list(''),references()]).then(async()=>{const id=new URLSearchParams(location.hash.split('?')[1]||'').get('record');if(id)await open(id)}).catch(e=>{setListFeedback({error:e.message});onConnection(false)})
  const sync=()=>{if(location.hash.slice(1).split('?')[0]!=='inventory-transfers')return;const id=new URLSearchParams(location.hash.split('?')[1]||'').get('record');if(id)void open(id);else if(lock.current)pendingOpen.current='';else fresh()}
  window.addEventListener('hashchange',sync);return()=>window.removeEventListener('hashchange',sync)
 },[])
 useEffect(()=>{
  let current=true;setDestinations([])
  if(editing&&draft.source_position_id)api<Position[]>(API+'/destinations?source_position_id='+encodeURIComponent(draft.source_position_id)).then(data=>{if(current){setDestinations(data);setSourceFeedback({})}}).catch(e=>{if(current)setSourceFeedback({error:e.message})})
  return()=>{current=false}
 },[draft.source_position_id,editing,sources])
 useEffect(()=>{if(review&&!dialog.current?.open){dialog.current?.showModal();reviewCancel.current?.focus()}},[review])
 function cancelEdit(){setFeedback({});setSourceFeedback({});if(selected){setDraft(from(selected));setEditing(false)}else fresh()}
 useEffect(()=>{const escape=(e:KeyboardEvent)=>{if(e.key==='Escape'&&!lock.current&&!dialog.current?.open&&editing)cancelEdit()};window.addEventListener('keydown',escape);return()=>window.removeEventListener('keydown',escape)},[editing,selected])
 function change(key:keyof Draft,value:string){setDraft(d=>({...d,[key]:value}))}
 async function search(e:FormEvent){e.preventDefault();setApplied(query);appliedQuery.current=query;try{await list(query)}catch(e){setListFeedback({error:(e as Error).message})}}
 async function reloadSources(){if(!enter())return;setSourceFeedback({});try{await references();if(selected&&!editing)accept(await api<Transfer>(API+'/'+selected.id));setSourceFeedback({notice:'已重新讀取兩倉真實庫存與凍結狀態；未建立目的位置或產生庫存異動。'})}catch(e){setSourceFeedback({error:(e as Error).message})}finally{leave()}}
 async function save(e:FormEvent){
  e.preventDefault();if(!enter())return;setFeedback({})
  try{if(!chosen||!destination)throw Error('請選擇真實來源庫存位置與不同的目的倉庫。')
   const {item_id:_,source_warehouse_id:__,...input}=draft
   const r=await api<Transfer>(API+(selected?'/'+selected.id:''),{method:selected?'PUT':'POST',body:JSON.stringify({...input,note:draft.note||null,...(selected?{row_version:selected.row_version}:{request_key:createKey.current})})})
   accept(r);attempts.current.delete(r.id);cancelAttempts.current.delete(r.id);setEditing(false);remember(r.id);await Promise.all([list(),references()]);setFeedback({notice:'調撥草稿已儲存；兩倉庫存未變更，未建立目的位置。'+(r.blockers.length?' 尚未符合過帳條件：'+r.blockers.join(' '):' 請另行檢視並明確確認過帳。')})
  }catch(e){setFeedback({error:(e as Error).message})}finally{leave()}
 }
 async function remove(){
  if(!selected||!window.confirm('確定刪除未過帳調撥草稿「'+selected.document_no+'」？')||!enter())return;setFeedback({})
  try{await api(API+'/'+selected.id+'?row_version='+selected.row_version,{method:'DELETE'});attempts.current.delete(selected.id);fresh();await Promise.all([list(),references()]);setFeedback({notice:'調撥草稿已刪除；兩倉庫存與原有歷史未變更。'})}catch(e){setFeedback({error:(e as Error).message})}finally{leave()}
 }
 async function cancelDocument(){
  if(!selected||!window.confirm('確定取消調撥草稿「'+selected.document_no+'」？這不會過帳或解除盤點凍結。')||!enter())return;setFeedback({})
  const body=cancelAttempts.current.get(selected.id)??{request_key:crypto.randomUUID(),row_version:selected.row_version};cancelAttempts.current.set(selected.id,body)
  try{const r=await api<Transfer>(API+'/'+selected.id+'/cancel',{method:'POST',body:JSON.stringify(body)});accept(r);setEditing(false);attempts.current.delete(r.id);cancelAttempts.current.delete(r.id);await list();setFeedback({notice:'調撥單已取消；零庫存分錄，兩倉庫存與原始歷史未變更。'})}catch(e){setFeedback({error:(e as Error).message})}finally{leave()}
 }
 async function inspect(){
  if(!selected||!enter())return;setFeedback({});setReviewFeedback({})
  try{const current=await api<Transfer>(API+'/'+selected.id);accept(current)
   if(current.state!=='DRAFT'){setFeedback({notice:'此調撥單已結束，已讀取原始結果；沒有再次過帳。'});await list();return}
   const pending=attempts.current.get(selected.id)
   if(pending){setReview(pending.review);setReviewFeedback({notice:'保留上次原請求以安全重試；也可取消後重新讀取實際結果。'})}else setReview(await api<Review>(API+'/'+selected.id+'/posting-review'))
  }catch(e){setFeedback({error:(e as Error).message})}finally{leave()}
 }
 function closeReview(){if(lock.current)return;dialog.current?.close();setReview(null);setReviewFeedback({})}
 async function post(){
  if(!review?.confirmation_token||!review.posting_allowed||!enter())return;setReviewFeedback({})
  const attempt=attempts.current.get(review.transfer.id)??{id:review.transfer.id,body:{request_key:crypto.randomUUID(),row_version:review.transfer.row_version,confirmation_token:review.confirmation_token},review};attempts.current.set(attempt.id,attempt)
  try{const r=await api<Transfer>(API+'/'+attempt.id+'/post',{method:'POST',body:JSON.stringify(attempt.body)});accept(r);setEditing(false);remember(r.id);dialog.current?.close();setReview(null);setFeedback({notice:'倉庫調撥已原子過帳：來源扣庫、目的入庫共兩筆真實分錄；公司同品項總量不變。'})
   try{await Promise.all([list(),references()])}catch(e){setFeedback({error:'調撥已過帳，但清單重新讀取失敗：'+(e as Error).message})}
  }catch(e){setReviewFeedback({error:(e as Error).message+' 尚未取得成功回應；可保留原請求重試，或取消後重新讀取核對結果。'})}finally{leave()}
 }
 const actions=<div className="form-actions" data-feedback-group>
  <ActionFeedback {...feedback} busy={busy} onReload={selected?()=>{attempts.current.delete(selected.id);cancelAttempts.current.delete(selected.id);void open(selected.id)}:undefined}/>
  {editing?<button className="button primary" type="submit" disabled={busy||!loaded}>{selected?'儲存變更':'儲存調撥草稿'}</button>:canEdit&&<button className="button primary" type="button" disabled={busy} onClick={()=>{setDraft(from(selected!));setEditing(true);setFeedback({})}}>編輯調撥草稿</button>}
  {canEdit&&<button className="button danger" type="button" disabled={busy} onClick={()=>void remove()}>刪除調撥草稿</button>}
  {editing&&<button className="button" type="button" disabled={busy} onClick={cancelEdit}>取消</button>}
  {!editing&&canEdit&&<><button className="button primary" type="button" disabled={busy} onClick={()=>void inspect()}>檢視倉庫調撥確認</button><button className="button danger" type="button" disabled={busy} onClick={()=>void cancelDocument()}>取消調撥單</button></>}
  {selected&&<button className="button" type="button" disabled={busy} onClick={()=>{attempts.current.delete(selected.id);cancelAttempts.current.delete(selected.id);void open(selected.id)}}>重新讀取調撥單</button>}
 </div>
 return <>
  <div className="page-heading"><div><div className="eyebrow">INVENTORY / INTERWAREHOUSE TRANSFERS</div><h1>倉庫調撥管理</h1><p>將同一真實品項與原批次直接調往另一倉庫；草稿不產生庫存異動。</p></div><button className="button primary new-button" type="button" disabled={busy||!loaded} onClick={fresh}>＋ 新增倉庫調撥單</button></div>
  <div className="fg-receipt-notice" role="note"><strong>來源 -Q、目的 +Q；公司同品項／同單位總量不變。</strong><p>{policy?.message??'本機展示模式／尚未啟用正式電子簽核與身分權限驗證'}</p><p>兩倉沿用相同的原始批次身分；缺少的目的庫存位置僅在最後確認成功時建立。此為直接原子調撥。</p></div>
  <div className="po-layout"><section className="panel po-list" aria-label="倉庫調撥單清單"><div className="panel-title"><h2>倉庫調撥單清單 <span className="count">{rows.length}</span></h2></div>
   <div className="search-area" data-feedback-group><form className="search-bar" onSubmit={search}><label className="sr-only" htmlFor="trf-search">搜尋倉庫調撥單</label><input id="trf-search" value={query} onChange={e=>setQuery(e.target.value)} placeholder="搜尋單號、品項、倉庫、批號或原因"/><button type="submit">搜尋</button></form><ActionFeedback {...listFeedback}/></div>
   {rows.map(r=><div key={r.id} className={'po-list-row '+(selected?.id===r.id?'current-row':'')}><button className="record-link" type="button" disabled={busy} aria-label={'開啟倉庫調撥單 '+r.document_no} onClick={()=>void open(r.id)}><strong>{r.document_no}</strong><span>{r.source.item_code} · {r.source.item_name}</span><span>{r.source.warehouse_code} → {r.destination.warehouse_code} ／ {r.source.lot_code??'無批次'}</span><span>{stateName(r.state)} · {r.business_date}</span></button></div>)}
   {!rows.length&&<p className="empty-state">{applied?'沒有符合的調撥單。':'尚無倉庫調撥單；請由使用者建立並明確確認。'}</p>}
  </section><div className="po-detail"><section className="panel" aria-label="倉庫調撥單資料"><div className="panel-title"><h2>{selected?.document_no??'新增倉庫調撥單'}</h2><span data-testid="trf-state">{selected?stateName(selected.state):'DRAFT · 草稿'}</span></div>
   {editing?<form aria-label="編輯倉庫調撥單" onSubmit={save}><fieldset className="form-body po-fields fg-receipt-fields" disabled={busy||!loaded}>
    <div><label htmlFor="trf-number">倉庫調撥單號 *</label><input id="trf-number" required value={draft.document_no} onChange={e=>change('document_no',e.target.value)}/></div>
    <div><label htmlFor="trf-date">調撥日期 *</label><input id="trf-date" type="date" required value={draft.business_date} onChange={e=>change('business_date',e.target.value)}/></div>
    <div className="po-wide"><label htmlFor="trf-item">調撥品項 *</label><select id="trf-item" required value={draft.item_id} onChange={e=>setDraft(d=>({...d,item_id:e.target.value,source_warehouse_id:'',source_position_id:'',destination_warehouse_id:''}))}><option value="">請選擇已登記庫存的原料或成品</option>{items.map(s=><option key={s.item_id} value={s.item_id}>{s.item_code} · {s.item_name} · {s.item_kind==='RAW'?'原料':'成品'}</option>)}</select></div>
    <div><label htmlFor="trf-source-warehouse">來源倉庫 *</label><select id="trf-source-warehouse" required value={draft.source_warehouse_id} onChange={e=>setDraft(d=>({...d,source_warehouse_id:e.target.value,source_position_id:'',destination_warehouse_id:''}))}><option value="">請選擇此品項的既有倉庫</option>{warehouses.map(s=><option key={s.warehouse_id} value={s.warehouse_id}>{s.warehouse_code} · {s.warehouse_name}{s.frozen?'（已凍結）':''}</option>)}</select></div>
    <div><label htmlFor="trf-position">來源批號／庫存位置 *</label><select id="trf-position" required value={draft.source_position_id} onChange={e=>setDraft(d=>({...d,source_position_id:e.target.value,destination_warehouse_id:''}))}><option value="">請選擇真實原批次位置</option>{positions.map(s=><option key={s.position_id} value={s.position_id??''}>{s.lot_code??'無批次（依品項設定）'} · {s.current_stock_qty_base} {unit(s)}{s.frozen?' · 倉庫已凍結':''}</option>)}</select></div>
    <div className="po-wide"><label htmlFor="trf-destination">目的倉庫 *</label><select id="trf-destination" required value={draft.destination_warehouse_id} onChange={e=>change('destination_warehouse_id',e.target.value)}><option value="">請選擇不同的真實目的倉庫</option>{destinations.map(s=><option key={s.warehouse_id} value={s.warehouse_id}>{s.warehouse_code} · {s.warehouse_name}{s.frozen?'（已凍結）':''}</option>)}</select>{chosen&&destinations.length===0&&<p className="field-note">目前沒有可選的不同目的倉庫；請先在既有倉庫資料維護新增或啟用適當倉庫，再重新讀取。</p>}</div>
    {chosen&&<div className="po-wide"><PositionSummary position={chosen} side="source"/></div>}{destination&&<div className="po-wide"><PositionSummary position={destination} side="destination"/></div>}
    <div><label htmlFor="trf-quantity">調撥數量 *</label><input id="trf-quantity" type="text" inputMode="decimal" required value={draft.quantity} onChange={e=>change('quantity',e.target.value)}/><p className="field-note">必須大於 0；兩倉使用相同原品項、批次與基本單位。</p></div>
    <div className="po-wide"><label htmlFor="trf-reason">調撥原因 *</label><textarea id="trf-reason" required value={draft.reason} onChange={e=>change('reason',e.target.value)}/></div>
    <div className="po-wide"><label htmlFor="trf-note">調撥備註</label><textarea id="trf-note" value={draft.note} onChange={e=>change('note',e.target.value)}/></div>
    <p className="field-note po-wide">儲存草稿後，請另行檢視伺服器精確試算的兩筆分錄與兩倉預計庫存；只有明確確認才會過帳。</p>
   </fieldset><div className="form-actions" data-feedback-group><ActionFeedback {...sourceFeedback}/><button className="button" type="button" disabled={busy} onClick={()=>void reloadSources()}>重新讀取兩倉庫存</button></div>{actions}</form>:selected&&<>
    <PositionSummary position={selected.source} side="source"/><PositionSummary position={selected.destination} side="destination"/>
    <dl className="po-header-summary"><dt>倉庫調撥單</dt><dd data-testid="trf-number-value">{selected.document_no}</dd><dt>調撥日期</dt><dd>{selected.business_date}</dd><dt>調撥數量</dt><dd data-testid="trf-quantity-value">{selected.quantity} {unit(selected.source)}</dd><dt>調撥原因</dt><dd>{selected.reason}</dd><dt>調撥備註</dt><dd>{selected.note||'—'}</dd><dt>原批次身分</dt><dd className="fg-receipt-identity" data-testid="trf-original-lot">{selected.source.lot_id??'無批次（依品項設定）'}<br/>兩倉沿用同一原批次；未新增採購來源或製造產出。</dd><dt>過帳／取消時間</dt><dd>{selected.posted_at??selected.cancelled_at??'—'}</dd></dl>
    {selected.state==='POSTED'&&<><p className="field-note po-policy">以下為本次確認時的歷史過帳前後值；上方顯示目前庫存，後續異動不會改寫此快照。</p><BalanceSummary value={selected} source={selected.source} testPrefix="trf-posted"/></>}{actions}
   </>}
  </section>{selected&&!editing&&selected.state==='POSTED'&&<section className="panel" aria-label="真實調撥庫存分錄"><div className="panel-title"><h2>真實調撥庫存分錄 <span className="count">{selected.movements.length}</span></h2></div><p className="field-note po-policy">共同過帳識別：<span data-testid="trf-posting-id">{selected.posting_id}</span>。本張調撥的兩筆已過帳分錄不可修改或刪除。</p>
   {selected.movements.map(m=>{const p=m.leg_no===1?selected.source:selected.destination;return <dl className="po-header-summary fg-receipt-movement" id={'trf-entry-'+m.id} key={m.id} data-testid="trf-entry"><dt>{m.leg_no===1?'來源倉扣庫':'目的倉入庫'}</dt><dd data-testid="trf-entry-quantity">{signed(m.qty_base_delta)} {unit(p)}</dd><dt>倉庫／原批次</dt><dd>{p.warehouse_code} ／ {p.lot_code??'無批次'}</dd><dt>庫存位置識別</dt><dd className="fg-receipt-identity" data-testid="trf-entry-position">{m.position_id}</dd><dt>分錄識別</dt><dd className="fg-receipt-identity"><a href={'#inventory-transfers?record='+selected.id+'&entry='+m.id} onClick={()=>setTimeout(()=>document.getElementById('trf-entry-'+m.id)?.scrollIntoView({block:'center'}),0)}>{m.id}</a></dd></dl>})}
  </section>}</div></div>
  <dialog ref={dialog} className="fg-receipt-dialog" aria-labelledby="trf-review-title" onCancel={e=>{e.preventDefault();closeReview()}}>{review&&<>
   <h2 id="trf-review-title">檢視倉庫調撥確認</h2><p>{review.message}</p><PositionSummary position={review.source} side="source"/><PositionSummary position={review.destination} side="destination"/>
   <dl className="po-header-summary"><dt>倉庫調撥單／日期</dt><dd>{review.transfer.document_no} ／ {review.transfer.business_date}</dd><dt>調撥數量</dt><dd>{review.transfer.quantity} {unit(review.source)}</dd><dt>調撥原因</dt><dd>{review.transfer.reason}</dd><dt>調撥備註</dt><dd>{review.transfer.note||'—'}</dd><dt>真實原批次身分</dt><dd className="fg-receipt-identity">{review.source.lot_id??'無批次（依品項設定）'}</dd><dt>來源／目的位置識別</dt><dd className="fg-receipt-identity">{review.source.position_id}<br/>{review.destination.position_id??'尚未建立；僅在最後確認成功時建立'}</dd></dl>
   <BalanceSummary value={review} source={review.source} testPrefix="trf-review"/>
   <div className="form-actions" data-feedback-group><p className="field-note">確認會執行真實實體庫存過帳：在同一交易建立來源 -Q 與目的 +Q 兩筆分錄，全部成功才會儲存；公司同品項總量變動為 0。</p><ActionFeedback error={reviewFeedback.error||(!review.posting_allowed?review.message:'')} notice={reviewFeedback.notice}/><button className="button primary" type="button" disabled={busy||!review.posting_allowed||!review.confirmation_token} onClick={()=>void post()}>確認倉庫調撥過帳</button><button ref={reviewCancel} className="button" type="button" disabled={busy} onClick={closeReview}>取消</button></div>
  </>}</dialog>
 </>
}
