import {useEffect,useRef,useState,type FormEvent} from 'react'
import {api} from './api'
import {ActionFeedback} from './ActionFeedback'
import './MaterialIssues.css'

type Ref={id:string;code:string;name:string;row_version:number}
type Work={id:string;document_no:string;product_code:string;product_name:string;planned_qty_base:string;product_base_unit_code:string;row_version:number;approval_state:string;bom_id:string|null;routing_id:string|null;bom:{revision:string;row_version:number}|null;routing:{revision:string;row_version:number}|null}
type Component={id:string;line_no:number;material_id:string;row_version:number;usage_qty_base:string;purpose_note:string|null}
type Bom={id:string;components:Component[]}
type Origin={receipt_id:string;receipt_no:string;receipt_line_id:string;quantity_base:string}
type Position={id:string;material_id:string;material_code:string;material_name:string;warehouse_id:string;warehouse_code:string;warehouse_name:string;lot_id:string|null;lot_code:string|null;unit_id:string;unit_code:string;unit_name:string;physical_qty_base:string;receipt_origins:Origin[]}
type Policy={posting_allowed:boolean;authorization_basis:string|null;message:string}
type HeaderDraft={document_no:string;work_order_id:string;business_date:string;note:string}
type LineDraft={material_id:string;warehouse_id:string;position_id:string;bom_component_id:string;quantity:string;note:string}
type Line=Omit<LineDraft,'bom_component_id'>&{id:string;line_no:number;row_version:number;bom_component_id:string|null;bom_component_version:number|null;position:Position;component_purpose:string|null}
type Issue=HeaderDraft&{id:string;row_version:number;work_order_version:number;bom_id:string|null;bom_version:number|null;routing_version:number|null;work_order:Work;posting_state:string;posting_id:string|null;posted_at:string|null;posted_by:string|null;authorization_basis:string|null;policy:Policy;created_at:string;updated_at:string;created_by:string;lines:Line[];movements:{id:string;issue_line_id:string;position_id:string;qty_base_delta:string}[]}
type Feedback={error?:string;notice?:string}
type Editor={id:string|null;key:string;draft:LineDraft}
type PostingReview={issue:Issue;confirmation_token:string;expected_movements:{issue_line_id:string;position_id:string;qty_base_delta:string;unit_code:string}[]}
const LOCAL_DEMO_BASIS='LOCAL_DEMO_MANUAL'
const DEMO_DISCLOSURE='本機展示模式／尚未啟用正式電子簽核與身分權限驗證'
const blankHeader=():HeaderDraft=>({document_no:'',work_order_id:'',business_date:'',note:''})
const blankLine=():LineDraft=>({material_id:'',warehouse_id:'',position_id:'',bom_component_id:'',quantity:'',note:''})
const headerDraft=(r:Issue):HeaderDraft=>({document_no:r.document_no,work_order_id:r.work_order_id,business_date:r.business_date,note:r.note||''})
function recordFromHash(){return new URLSearchParams(location.hash.split('?')[1]||'').get('record')}
function remember(id?:string){history.replaceState(null,'','#material-issues'+(id?'?record='+encodeURIComponent(id):''))}

export function MaterialIssues({onConnection}:{onConnection:(online:boolean)=>void}){
 const [rows,setRows]=useState<Issue[]>([]),[works,setWorks]=useState<Work[]>([]),[materials,setMaterials]=useState<Ref[]>([]),[warehouses,setWarehouses]=useState<Ref[]>([]),[positions,setPositions]=useState<Position[]>([]),[boms,setBoms]=useState<Bom[]>([])
 const [selected,setSelected]=useState<Issue|null>(null),[draft,setDraft]=useState<HeaderDraft>(blankHeader),[editing,setEditing]=useState(false),[editor,setEditor]=useState<Editor|null>(null)
 const [query,setQuery]=useState(''),[applied,setApplied]=useState(''),[busy,setBusy]=useState(false),[loaded,setLoaded]=useState(false)
 const [headerKey,setHeaderKey]=useState(()=>crypto.randomUUID()),[postKey,setPostKey]=useState(()=>crypto.randomUUID())
 const [feedback,setFeedback]=useState<Feedback>({}),[lineFeedback,setLineFeedback]=useState<Feedback>({}),[listFeedback,setListFeedback]=useState<Feedback>({}),[postFeedback,setPostFeedback]=useState<Feedback>({}),[sourceFeedback,setSourceFeedback]=useState<Feedback>({})
 const [policy,setPolicy]=useState<Policy|null>(null)
 const [lineActionId,setLineActionId]=useState<string|null>(null),[openError,setOpenError]=useState<{id:string;message:string}|null>(null)
 const [review,setReview]=useState<PostingReview|null>(null),[reviewKey,setReviewKey]=useState(''),[reviewAttempted,setReviewAttempted]=useState(false),[reviewFeedback,setReviewFeedback]=useState<Feedback>({})
 const postingInFlight=useRef(false),reviewInFlight=useRef(false),reviewDialog=useRef<HTMLElement>(null),reviewCancel=useRef<HTMLButtonElement>(null),reviewTrigger=useRef<HTMLButtonElement>(null)
 useEffect(()=>{
  if(!review)return
  if(postingInFlight.current)reviewDialog.current?.focus();else reviewCancel.current?.focus()
  const onKeyDown=(event:KeyboardEvent)=>{
   if(event.key==='Escape'){
    event.preventDefault()
    if(!postingInFlight.current)closeReview()
   }
   if(event.key==='Tab'){
    const controls=reviewDialog.current?.querySelectorAll<HTMLButtonElement>('button:not([disabled])')
    if(!controls?.length){event.preventDefault();return}
    const first=controls[0],last=controls[controls.length-1]
    if(event.shiftKey&&document.activeElement===first){event.preventDefault();last.focus()}
    else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first.focus()}
   }
  }
  document.addEventListener('keydown',onKeyDown)
  return()=>{document.removeEventListener('keydown',onKeyDown);reviewTrigger.current?.focus()}
 },[review,reviewAttempted])
 async function references(){
  const [w,m,h,p,b,pol]=await Promise.all([api<Work[]>('/work-orders'),api<Ref[]>('/raw-materials'),api<Ref[]>('/warehouses'),api<Position[]>('/material-issues/stock-options'),api<Bom[]>('/boms'),api<Policy>('/material-issues/policy')])
  setWorks(w);setMaterials(m);setWarehouses(h);setPositions(p);setBoms(b);setPolicy(pol);setLoaded(true);onConnection(true)
 }
 async function list(q=applied){setRows(await api<Issue[]>('/material-issues?q='+encodeURIComponent(q)))}
 async function refresh(id:string){const r=await api<Issue>('/material-issues/'+id);setSelected(r);setDraft(headerDraft(r));setPolicy(r.policy);await list();return r}
 async function open(id:string){
  setBusy(true);setListFeedback({});setOpenError(null);setLineActionId(null);setFeedback({});setLineFeedback({});setPostFeedback({});setEditor(null)
  try{await refresh(id);setEditing(false);setPostKey(crypto.randomUUID());remember(id)}
  catch(e){setOpenError({id,message:(e as Error).message});if(!rows.some(r=>r.id===id))setListFeedback({error:(e as Error).message})}finally{setBusy(false)}
 }
 useEffect(()=>{(async()=>{try{await references();await list('');const id=recordFromHash();if(id)await open(id)}catch(e){setListFeedback({error:(e as Error).message});onConnection(false)}})()},[])
 function newIssue(){setSelected(null);setDraft(blankHeader());setEditing(true);setEditor(null);setFeedback({});setLineFeedback({});setPostFeedback({});setHeaderKey(crypto.randomUUID());remember()}
 function cancelHeader(){setDraft(selected?headerDraft(selected):blankHeader());setEditing(false);setFeedback({notice:'已取消未儲存的表頭變更，業務資料未異動。'})}
 async function saveHeader(e:FormEvent){
  e.preventDefault();setBusy(true);setFeedback({})
  try{
   const wo=works.find(w=>w.id===draft.work_order_id)
   if(!wo)throw Error('請明確選取製造工單。')
   const body={...draft,note:draft.note||null,work_order_version:wo.row_version,...(selected?{row_version:selected.row_version}:{request_key:headerKey})}
   const row=await api<Issue>('/material-issues'+(selected?'/'+selected.id:''),{method:selected?'PUT':'POST',body:JSON.stringify(body)})
   await refresh(row.id);setEditing(false);setPostKey(crypto.randomUUID());remember(row.id);setFeedback({notice:'領料草稿已儲存；庫存未異動。'})
  }catch(e){setFeedback({error:(e as Error).message})}finally{setBusy(false)}
 }
 async function removeHeader(){
  if(!selected||!confirm('確定刪除此領料草稿及其未過帳明細？'))return
  setBusy(true);setFeedback({})
  try{await api('/material-issues/'+selected.id+'?row_version='+selected.row_version,{method:'DELETE'});setSelected(null);setDraft(blankHeader());setEditor(null);setEditing(false);remember();await list();setFeedback({notice:'領料草稿已刪除，庫存未異動。'})}
  catch(e){setFeedback({error:(e as Error).message})}finally{setBusy(false)}
 }
 function editLine(row?:Line){setLineActionId(null);setLineFeedback({});setEditor({id:row?.id||null,key:crypto.randomUUID(),draft:row?{material_id:row.material_id,warehouse_id:row.warehouse_id,position_id:row.position_id,bom_component_id:row.bom_component_id||'',quantity:row.quantity,note:row.note||''}:blankLine()})}
 function changeLine(key:keyof LineDraft,value:string){
  setEditor(ed=>ed?{...ed,draft:{...ed.draft,[key]:value,...(key==='material_id'?{position_id:'',bom_component_id:''}:key==='warehouse_id'?{position_id:''}:{})}}:null)
 }
 async function saveLine(e:FormEvent){
  e.preventDefault();if(!selected||!editor)return;setBusy(true);setLineFeedback({})
  try{
   const component=boms.find(b=>b.id===selected.bom_id)?.components.find(c=>c.id===editor.draft.bom_component_id)
   const line=selected.lines.find(l=>l.id===editor.id)
   const body={...editor.draft,note:editor.draft.note||null,bom_component_id:editor.draft.bom_component_id||null,bom_component_version:component?.row_version??null,document_version:selected.row_version,...(line?{row_version:line.row_version}:{request_key:editor.key})}
   await api('/material-issues/'+selected.id+'/lines'+(line?'/'+line.id:''),{method:line?'PUT':'POST',body:JSON.stringify(body)})
   await refresh(selected.id);setEditor(null);setPostKey(crypto.randomUUID());setLineFeedback({notice:'領料明細已儲存；草稿尚未扣庫。'})
  }catch(e){setLineFeedback({error:(e as Error).message})}finally{setBusy(false)}
 }
 async function removeLine(line:Line){
  if(!selected||!confirm('確定刪除此未過帳領料明細？'))return
  setBusy(true);setLineActionId(line.id);setLineFeedback({})
  try{await api('/material-issues/'+selected.id+'/lines/'+line.id+'?row_version='+line.row_version+'&document_version='+selected.row_version,{method:'DELETE'});await refresh(selected.id);setEditor(null);setPostKey(crypto.randomUUID());setLineActionId(null);setLineFeedback({notice:'領料草稿明細已刪除，庫存未異動。'})}
  catch(e){setLineFeedback({error:(e as Error).message})}finally{setBusy(false)}
 }
 async function postQa(){
  if(!selected)return
  if(!confirm('僅限隔離 QA 測試授權：確定進行真實扣庫？這不代表 Owner 生產核准。'))return
  setBusy(true);setPostFeedback({})
  try{await api('/material-issues/'+selected.id+'/post',{method:'POST',body:JSON.stringify({request_key:postKey,row_version:selected.row_version,work_order_version:selected.work_order_version})});await refresh(selected.id);await references();setPostFeedback({notice:'隔離 QA 領料已真正過帳並扣庫；正式生產核准仍待 Owner 決定。'})}
  catch(e){setPostFeedback({error:(e as Error).message})}finally{setBusy(false)}
 }
 async function reviewPosting(){
  if(!selected||reviewInFlight.current||postingInFlight.current)return
  reviewInFlight.current=true;setBusy(true);setPostFeedback({});setReviewFeedback({})
  try{
   const fresh=await api<PostingReview>('/material-issues/'+selected.id+'/posting-review')
   setSelected(fresh.issue);setDraft(headerDraft(fresh.issue));setPolicy(fresh.issue.policy)
   if(fresh.issue.posting_id){setReview(null);setPostFeedback({notice:'這張領料單已有過帳紀錄，請查看實際分錄與庫存。'});return}
   if(!fresh.issue.policy.posting_allowed||fresh.issue.policy.authorization_basis!==LOCAL_DEMO_BASIS)throw Error(fresh.issue.policy.message)
   setReviewKey(crypto.randomUUID());setReviewAttempted(false);setReview(fresh)
  }catch(e){setPostFeedback({error:(e as Error).message})}finally{reviewInFlight.current=false;setBusy(false)}
 }
 function closeReview(){
  if(postingInFlight.current)return
  setReview(null);setReviewFeedback({})
  setPostFeedback({notice:reviewAttempted?'已關閉確認畫面；請重新讀取單據與庫存，確認上次請求的實際結果。':'已取消領料過帳確認；未送出過帳請求，庫存未異動。'})
 }
 async function confirmPosting(){
  if(!review||postingInFlight.current)return
  postingInFlight.current=true;setBusy(true);setReviewAttempted(true);setReviewFeedback({})
  try{
   const row=await api<Issue>('/material-issues/'+review.issue.id+'/post',{method:'POST',body:JSON.stringify({request_key:reviewKey,row_version:review.issue.row_version,work_order_version:review.issue.work_order_version,confirmation_token:review.confirmation_token})})
   setSelected(row);setDraft(headerDraft(row));setPolicy(row.policy);setReview(null)
   setPostFeedback({notice:'領料已正式過帳，實際庫存已扣除；本機展示例外不代表正式電子簽核或完整 D06 核准。'})
   try{await list();await references()}catch(e){setSourceFeedback({error:'領料已過帳，但參考資料重新讀取失敗：'+(e as Error).message})}
  }catch(e){setReviewFeedback({error:(e as Error).message+' 如來源已更新，請取消並重新開啟確認畫面。'})}finally{postingInFlight.current=false;setBusy(false)}
 }
 async function checkPolicy(){setBusy(true);setPostFeedback({});try{const p=await api<Policy>('/material-issues/policy');setPolicy(p);setPostFeedback({notice:p.message})}catch(e){setPostFeedback({error:(e as Error).message})}finally{setBusy(false)}}
 async function reloadSources(){setBusy(true);setSourceFeedback({});try{await references();setSourceFeedback({notice:'參考資料與實體庫存已更新；請核對明確選取的來源。'})}catch(e){setSourceFeedback({error:(e as Error).message})}finally{setBusy(false)}}
 const posted=selected?.posting_state==='POSTED',locked=!!selected?.posting_id,work=selected?.work_order,localDemo=policy?.authorization_basis===LOCAL_DEMO_BASIS
 const offered=editor?positions.filter(p=>p.material_id===editor.draft.material_id&&p.warehouse_id===editor.draft.warehouse_id):[]
 const chosen=editor?positions.find(p=>p.id===editor.draft.position_id):null
 const components=boms.find(b=>b.id===selected?.bom_id)?.components.filter(c=>c.material_id===editor?.draft.material_id)||[]
 return <div className="issue-page">
  <div className="page-heading"><div><div className="eyebrow">MANUFACTURING / MATERIAL ISSUE</div><h1>生產領料管理</h1><p>{localDemo?'本機展示領料須逐張核對並明確確認；保存草稿不會扣庫。':'先保存領料草稿；正式扣庫需另有已核准的製造授權政策。'}</p></div><button className="button primary" disabled={busy||!loaded} onClick={newIssue}>＋ 新增領料單</button></div>
  <div className="content-grid">
   <section className="panel list-panel" aria-label="領料單清單"><div className="panel-title"><h2>領料單清單 <span className="count">{rows.length}</span></h2><span>草稿與實際過帳分開</span></div>
    <div className="search-area" data-feedback-group><form className="search-bar" onSubmit={async e=>{e.preventDefault();setApplied(query);setListFeedback({});try{await list(query)}catch(e){setListFeedback({error:(e as Error).message})}}}><label className="sr-only" htmlFor="mi-search">搜尋領料單或工單</label><input id="mi-search" value={query} onChange={e=>setQuery(e.target.value)} placeholder="搜尋領料單號、工單或成品"/><button>搜尋</button></form><ActionFeedback {...listFeedback}/></div>
    <div className="table-wrap"><table><thead><tr><th>領料單／工單</th><th>狀態</th></tr></thead><tbody>{rows.map(r=><tr key={r.id} className={selected?.id===r.id?'current-row':''}><td data-feedback-group><button className="record-link" disabled={busy} aria-label={'開啟領料單 '+r.document_no} onClick={()=>open(r.id)}><strong>{r.document_no}</strong><span>{r.work_order.document_no} · {r.work_order.product_code}</span></button><ActionFeedback error={openError?.id===r.id?openError.message:''}/></td><td>{r.posting_state==='POSTED'?'已過帳':r.posting_id?'過帳紀錄需檢查':'草稿／未扣庫'}<br/>{r.lines.length} 筆明細</td></tr>)}</tbody></table></div>
    {!rows.length&&<div className="empty-state"><h3>目前沒有領料單</h3><p>新增草稿不會異動庫存。</p></div>}
   </section>
   <section className="panel editor-panel" aria-label="領料單資料"><div className="panel-title"><h2>{editing?(selected?'編輯領料表頭':'新增領料表頭'):'領料單資料'}</h2><span>{posted?'POSTED · 不可變歷史':locked?'PREPARING · 請檢查':'DRAFT'}</span></div>
    {editing?<form onSubmit={saveHeader}><div className="form-body">
     <label htmlFor="mi-number">領料單號 *</label><input id="mi-number" required value={draft.document_no} onChange={e=>setDraft({...draft,document_no:e.target.value})}/>
     <label htmlFor="mi-work">製造工單 *</label><select id="mi-work" required disabled={!!selected?.lines.length} value={draft.work_order_id} onChange={e=>setDraft({...draft,work_order_id:e.target.value})}><option value="">請明確選取工單</option>{works.map(w=><option key={w.id} value={w.id}>{w.document_no} · {w.product_code} · {w.planned_qty_base} {w.product_base_unit_code} · {w.approval_state}</option>)}</select>
     {selected?.lines.length?<p className="field-note">已有明細，不可改派工單；不會自動重掛來源。</p>:null}
     <label htmlFor="mi-date">領料日期 *</label><input id="mi-date" type="date" required value={draft.business_date} onChange={e=>setDraft({...draft,business_date:e.target.value})}/>
     <label htmlFor="mi-note">備註／用途</label><textarea id="mi-note" value={draft.note} onChange={e=>setDraft({...draft,note:e.target.value})}/>
    </div><div className="form-actions" data-feedback-group><ActionFeedback {...feedback}/><div className="button-row"><button className="button primary" disabled={busy}>{selected?'儲存表頭變更':'儲存領料草稿'}</button>{selected&&<button type="button" className="button danger" disabled={busy} onClick={removeHeader}>刪除領料草稿</button>}<button type="button" className="button secondary" disabled={busy} onClick={cancelHeader}>取消</button></div></div></form>:<>
     <div className="form-body">{selected?<dl className="po-header-summary"><dt>領料單號</dt><dd data-testid="mi-number-value">{selected.document_no}</dd><dt>製造工單／成品</dt><dd>{work?.document_no}<br/>{work?.product_code} · {work?.product_name}</dd><dt>計畫產出／工單狀態</dt><dd>{work?.planned_qty_base} {work?.product_base_unit_code} · {work?.approval_state}</dd><dt>來源資料版次</dt><dd>工單 {selected.work_order_version}<br/>{work?.bom?.revision||'未選取 BOM'} · BOM {selected.bom_version??'無'}<br/>{work?.routing?.revision||'未選取途程'} · 途程 {selected.routing_version??'無'}</dd><dt>領料日期</dt><dd>{selected.business_date}</dd><dt>備註</dt><dd>{selected.note||'—'}</dd><dt>實際狀態</dt><dd data-testid="mi-state">{posted?'已過帳／實際庫存已扣除':locked?'過帳紀錄尚未完成，請保留資料並檢查':'草稿／尚未領料過帳，庫存未異動'}</dd>{posted&&<><dt>授權依據</dt><dd>{selected.authorization_basis} · {selected.authorization_basis===LOCAL_DEMO_BASIS?'Owner 已授權本機展示例外；非完整 D06 核准':'非 Owner 生產核准'}</dd><dt>過帳識別／時間</dt><dd>{selected.posting_id}<br/>{selected.posted_at}</dd></>}</dl>:<p>請選取既有領料單，或新增草稿。</p>}</div>
     <div className="form-actions" data-feedback-group><ActionFeedback {...feedback}/>{selected&&!locked&&<div className="button-row"><button className="button primary" disabled={busy||!!editor} onClick={()=>{setDraft(headerDraft(selected));setEditing(true);setFeedback({})}}>編輯領料表頭</button><button className="button danger" disabled={busy||!!editor} onClick={removeHeader}>刪除領料草稿</button></div>}</div>
    </>}
   </section>
  </div>
  {selected&&<section className="panel" aria-label="領料明細"><div className="panel-title"><h2>逐筆領料明細</h2><span>標準用量不是實際領料；本次數量由使用者明確輸入</span>{!locked&&!editing&&<button className="button secondary" disabled={busy||!!editor} onClick={()=>editLine()}>＋ 新增領料明細</button>}</div>
   <div className="table-wrap"><table><thead><tr><th>原料／用途</th><th>來源倉庫／批次</th><th>請領量／實體庫存</th><th>操作</th></tr></thead><tbody>{selected.lines.map(l=><tr key={l.id} data-testid={'mi-line-'+l.line_no}><td>{l.position.material_code} · {l.position.material_name}<br/>{l.note||'—'}<br/>{l.bom_component_id?'BOM 用料：'+(l.component_purpose||'未填用途'):'未指定 BOM 用料行'}</td><td>{l.position.warehouse_code} · {l.position.warehouse_name}<br/>{l.position.lot_code||'無批次（明確庫存位置）'}</td><td>{l.quantity} {l.position.unit_code}<br/>目前實體：{l.position.physical_qty_base} {l.position.unit_code}</td><td>{!locked&&!editing&&<div className="button-row" data-feedback-group><ActionFeedback {...(lineActionId===l.id?lineFeedback:{})}/><button className="button secondary" disabled={busy||!!editor} aria-label={'編輯領料明細 '+l.line_no} onClick={()=>editLine(l)}>編輯</button><button className="button danger" disabled={busy||!!editor} aria-label={'刪除領料明細 '+l.line_no} onClick={()=>removeLine(l)}>刪除</button></div>}<details><summary>來源與稽核</summary><p>明細：{l.id} · 資料版次 {l.row_version}<br/>位置：{l.position_id}<br/>原料批：{l.position.lot_id||'無批次'}<br/>BOM 行：{l.bom_component_id||'未指定'}</p>{l.position.receipt_origins.map(o=><p key={o.receipt_line_id}>批次來源收貨：{o.receipt_no} · {o.quantity_base} {l.position.unit_code}<br/>{o.receipt_line_id}</p>)}<p>這是來源批次的收貨關聯，未推定逐次入庫耗用分攤。</p></details></td></tr>)}</tbody></table></div>
   {editor?<form onSubmit={saveLine}><div className="form-body po-fields">
    <label htmlFor="mi-material">原物料 *<select id="mi-material" required value={editor.draft.material_id} onChange={e=>changeLine('material_id',e.target.value)}><option value="">請明確選取原料</option>{materials.map(m=><option key={m.id} value={m.id}>{m.code} · {m.name}</option>)}</select></label>
    <label htmlFor="mi-warehouse">來源倉庫 *<select id="mi-warehouse" required value={editor.draft.warehouse_id} onChange={e=>changeLine('warehouse_id',e.target.value)}><option value="">請明確選取倉庫</option>{warehouses.map(w=><option key={w.id} value={w.id}>{w.code} · {w.name}</option>)}</select></label>
    <label htmlFor="mi-position">來源批次／庫存位置 *<select id="mi-position" required value={editor.draft.position_id} onChange={e=>changeLine('position_id',e.target.value)}><option value="">請明確選取批次位置</option>{offered.map(p=><option key={p.id} value={p.id}>{p.lot_code||'無批次'} · 實體 {p.physical_qty_base} {p.unit_code}</option>)}</select></label>
    <label htmlFor="mi-component">BOM 用料來源（可留空）<select id="mi-component" value={editor.draft.bom_component_id} onChange={e=>changeLine('bom_component_id',e.target.value)}><option value="">未指定；不會自動選取或視為核准替代料</option>{components.map(c=><option key={c.id} value={c.id}>{c.line_no} · {c.purpose_note||'未填用途'} · 標準用量 {c.usage_qty_base}</option>)}</select></label>
    <label htmlFor="mi-quantity">本次實際請領量 *<input id="mi-quantity" type="text" required value={editor.draft.quantity} onChange={e=>changeLine('quantity',e.target.value)}/></label>
    <label htmlFor="mi-line-note">明細用途／備註<input id="mi-line-note" value={editor.draft.note} onChange={e=>changeLine('note',e.target.value)}/></label>
    {chosen&&<p>來源：{chosen.material_code}／{chosen.warehouse_code}／{chosen.lot_code||'無批次'} · 實體 {chosen.physical_qty_base} {chosen.unit_code}。庫存顯示僅供參考，不是保留或核准。</p>}
    {!offered.length&&editor.draft.material_id&&editor.draft.warehouse_id&&<p>所選原料／倉庫沒有既有庫存位置；不會自動建立批次。</p>}
   </div><div className="form-actions" data-feedback-group><ActionFeedback {...lineFeedback}/><div className="button-row"><button className="button primary" disabled={busy}>儲存領料明細</button><button type="button" className="button secondary" disabled={busy} onClick={()=>{setEditor(null);setLineFeedback({notice:'已取消未儲存的明細變更，業務資料未異動。'})}}>取消</button></div></div></form>:<div className="form-actions" data-feedback-group><ActionFeedback {...(!lineActionId?lineFeedback:{})}/></div>}
  </section>}
  <section className="panel" aria-label="領料過帳政策"><div className="panel-title"><h2>實際過帳與政策狀態</h2><span>{policy?.authorization_basis||'正式製造授權未核定'}</span></div><div className="form-body"><p>{policy?.message||'正在讀取後端政策狀態…'}</p>{localDemo&&<p className="issue-demo-disclosure">{DEMO_DISCLOSURE}</p>}<p>{localDemo?'Owner 僅已授權本機展示的逐張人工確認領料；實際出庫須為正向數量，且不得使同倉同批庫存為負。':'D04／D05／D06／D07／D08／D10 尚待核定：數值資格、共用供應、核准發放、批次及 BOM 有效版本政策。'} 保存草稿不是正式領料，也不建立庫存保留。正式電子簽核、身分權限、沖回及更正政策仍待核定。</p>{posted&&<><p>已過帳的表頭與明細不可直接編輯或刪除；請保留原始分錄。</p><p>MRP 仍為毛需求減實體庫存的草稿試算，尚未依已領料量沖減剩餘工單需求；不可視為正式剩餘採購義務。</p><div data-testid="mi-movements">{selected?.movements.map(m=>{const line=selected.lines.find(l=>l.id===m.issue_line_id);return <p key={m.id}>實際扣庫分錄：{m.qty_base_delta} {line?.position.unit_code} · 明細 {line?.line_no}<br/>{line?.position.material_code}／{line?.position.warehouse_code}／{line?.position.lot_code||'無批次'} · 目前實體庫存 {line?.position.physical_qty_base} {line?.position.unit_code}<br/>位置 {m.position_id}</p>})}</div></>}</div>
   <div className="form-actions" data-feedback-group><ActionFeedback {...postFeedback}/><div className="button-row">{selected&&!locked&&(policy?.posting_allowed?(localDemo?<button ref={reviewTrigger} type="button" className="button primary" disabled={busy||editing||!!editor} onClick={reviewPosting}>確認領料過帳</button>:policy.authorization_basis==='CONTROLLED_QA_ONLY'?<button type="button" className="button primary" disabled={busy||editing||!!editor} onClick={postQa}>QA 測試過帳（非生產核准）</button>:<button type="button" className="button secondary" disabled>正式領料過帳（政策待核定）</button>):<button type="button" className="button secondary" disabled>正式領料過帳（政策待核定）</button>)}<button type="button" className="button secondary" disabled={busy} onClick={checkPolicy}>檢查過帳政策</button></div></div>
  </section>
  <div className="form-actions" data-feedback-group><ActionFeedback {...sourceFeedback}/><button className="button secondary" disabled={busy} onClick={reloadSources}>重新讀取工單與庫存參考</button></div>
  {selected&&<details className="panel form-body"><summary>單據系統稽核</summary><p>領料單 UUID：{selected.id}<br/>資料版次：{selected.row_version}<br/>建立：{selected.created_at} · {selected.created_by}<br/>更新：{selected.updated_at}<br/>實際過帳操作員：{selected.posted_by||'尚未過帳'}</p></details>}
  {review&&<div className="modal-backdrop issue-confirm-backdrop"><section ref={reviewDialog} className="modal issue-post-dialog" role="dialog" aria-modal="true" aria-labelledby="mi-post-title" aria-describedby="mi-post-description" tabIndex={-1}>
   <div className="panel-title"><h2 id="mi-post-title">確認領料過帳</h2><span>{reviewAttempted?'已送出確認，請核對伺服器結果':'本機展示例外 · 尚未扣庫'}</span></div>
   <div className="modal-body"><p id="mi-post-description">請核對以下實際出庫內容。只有按下「確認並扣庫存」才會送出過帳請求；取消或關閉此畫面不會送出新的過帳，也不會撤銷已完成的過帳。</p><p className="issue-demo-disclosure">{DEMO_DISCLOSURE}</p>
    <dl className="po-header-summary"><dt>領料單</dt><dd>{review.issue.document_no} · 資料版次 {review.issue.row_version}</dd><dt>製造工單／成品</dt><dd>{review.issue.work_order.document_no}<br/>{review.issue.work_order.product_code} · {review.issue.work_order.product_name}<br/>{review.issue.work_order.approval_state} · 工單資料版次 {review.issue.work_order_version}</dd><dt>業務日期</dt><dd>{review.issue.business_date}</dd></dl>
    <div className="table-wrap"><table><thead><tr><th>原物料／明細</th><th>來源倉庫／批次</th><th>本次出庫量</th><th>預期庫存分錄</th><th>目前實體庫存</th></tr></thead><tbody>{review.issue.lines.map(line=>{const movement=review.expected_movements.find(m=>m.issue_line_id===line.id);return <tr key={line.id}><td>{line.position.material_code} · {line.position.material_name}<br/>明細 {line.line_no} · 資料版次 {line.row_version}</td><td>{line.position.warehouse_code} · {line.position.warehouse_name}<br/>{line.position.lot_code||'無批次（明確庫存位置）'}</td><td>{line.quantity} {line.position.unit_code}</td><td>{movement?movement.qty_base_delta+' '+movement.unit_code:'未提供，不能確認過帳'}</td><td>{line.position.physical_qty_base} {line.position.unit_code}</td></tr>})}</tbody></table></div>
    <p>同一庫存位置重複出現時，共用同一筆實體庫存，不是各行獨立可用。後端會在同一交易中合計所有明細並重新檢查；任一行不通過即整張不過帳。</p><p>這是 Owner 已授權的本機展示例外；不會把工單改為 RELEASED 或 APPROVED，也不代表完整 D06 電子簽核、角色或身分權限已核准。</p>
   </div>
   <div className="form-actions" data-feedback-group><ActionFeedback {...reviewFeedback}/><div className="button-row"><button type="button" className="button primary" disabled={busy||!review.confirmation_token||!review.issue.lines.length||review.issue.lines.some(line=>!review.expected_movements.some(m=>m.issue_line_id===line.id))} onClick={confirmPosting}>{busy?'過帳處理中…':'確認並扣庫存'}</button><button ref={reviewCancel} type="button" className="button secondary" disabled={busy} onClick={closeReview}>取消</button></div></div>
  </section></div>}
 </div>
}
