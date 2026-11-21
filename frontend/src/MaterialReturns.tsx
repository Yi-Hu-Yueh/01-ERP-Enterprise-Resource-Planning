import {useEffect,useRef,useState,type FormEvent,type ReactNode} from 'react'
import {api} from './api'
import {ActionFeedback} from './ActionFeedback'
import './MaterialReturns.css'

type Work={id:string;document_no:string;product_code:string;product_name:string;row_version:number;approval_state:string}
type Position={id:string;material_code:string;material_name:string;warehouse_code:string;warehouse_name:string;lot_code:string|null;unit_code:string;physical_qty_base:string}
type Source={origin_line_id:string;origin_line_version:number;issue_id:string;issue_no:string;issue_line_no:number;issue_posting_id:string;issue_posted_at:string;work_order_id:string;work_order_no:string;work_order_version:number;issued_qty_base:string;returned_qty_base:string;returnable_qty_base:string;position:Position}
type Policy={posting_allowed:boolean;authorization_basis:string|null;message:string}
type HeaderDraft={document_no:string;work_order_id:string;business_date:string;note:string}
type LineDraft={origin_line_id:string;quantity:string;note:string}
type Line=LineDraft&{id:string;line_no:number;row_version:number;origin_line_version:number;proposed_qty_base:string;remaining_after_proposal_qty_base:string;source:Source}
type MaterialReturn=HeaderDraft&{id:string;row_version:number;work_order_version:number;work_order:Work;posting_state:string;posting_id:string|null;posted_at:string|null;posted_by:string|null;authorization_basis:string|null;policy:Policy;created_at:string;updated_at:string;created_by:string;lines:Line[];movements:{id:string;return_line_id:string;position_id:string;qty_base_delta:string}[]}
type Review={material_return:MaterialReturn;confirmation_token:string|null;expected_movements:{return_line_id:string;origin_line_id:string;position_id:string;qty_base_delta:string;unit_code:string;current_stock_qty_base:string;expected_stock_qty_base:string}[]}
type PostBody={request_key:string;row_version:number;work_order_version:number;confirmation_token:string}
type Attempt={review:Review;body:PostBody}
type Feedback={error?:string;notice?:string}
type Editor={id:string|null;key:string;issue_id:string;draft:LineDraft}
const DISCLOSURE='本機展示模式／尚未啟用正式電子簽核與身分權限驗證'
const blankHeader=():HeaderDraft=>({document_no:'',work_order_id:'',business_date:'',note:''})
const headerOf=(r:MaterialReturn):HeaderDraft=>({document_no:r.document_no,work_order_id:r.work_order_id,business_date:r.business_date,note:r.note||''})
const positive=(value:string)=>value.startsWith('-')||value.startsWith('+')?value:'+'+value
const recordFromHash=()=>new URLSearchParams(location.hash.split('?')[1]||'').get('record')
function remember(id?:string){history.replaceState(null,'','#material-returns'+(id?'?record='+encodeURIComponent(id):''))}

export function MaterialReturns({onConnection}:{onConnection:(online:boolean)=>void}){
 const [rows,setRows]=useState<MaterialReturn[]>([]),[works,setWorks]=useState<Work[]>([]),[sources,setSources]=useState<Source[]>([]),[policy,setPolicy]=useState<Policy|null>(null)
 const [selected,setSelected]=useState<MaterialReturn|null>(null),[draft,setDraft]=useState<HeaderDraft>(blankHeader),[editing,setEditing]=useState(false),[editor,setEditor]=useState<Editor|null>(null)
 const [query,setQuery]=useState(''),[applied,setApplied]=useState(''),[busy,setBusy]=useState(false),[loaded,setLoaded]=useState(false),[headerKey,setHeaderKey]=useState(()=>crypto.randomUUID())
 const [feedback,setFeedback]=useState<Feedback>({}),[lineFeedback,setLineFeedback]=useState<Feedback>({}),[listFeedback,setListFeedback]=useState<Feedback>({}),[postFeedback,setPostFeedback]=useState<Feedback>({}),[sourceFeedback,setSourceFeedback]=useState<Feedback>({})
 const [lineActionId,setLineActionId]=useState<string|null>(null),[openError,setOpenError]=useState<{id:string;message:string}|null>(null)
 const [review,setReview]=useState<Review|null>(null),[reviewFeedback,setReviewFeedback]=useState<Feedback>({}),[attempted,setAttempted]=useState(false)
 const postingInFlight=useRef(false),reviewInFlight=useRef(false),attempts=useRef(new Map<string,Attempt>()),reviewBody=useRef<PostBody|null>(null)
 const reviewDialog=useRef<HTMLElement>(null),reviewCancel=useRef<HTMLButtonElement>(null),reviewTrigger=useRef<HTMLButtonElement>(null)
 const locked=!!selected?.posting_id,posted=selected?.posting_state==='POSTED'

 useEffect(()=>{
  if(!review)return
  reviewCancel.current?.focus()
  const keydown=(event:KeyboardEvent)=>{
   if(event.key==='Escape'){event.preventDefault();if(!postingInFlight.current)closeReview()}
   if(event.key==='Tab'){
    const controls=reviewDialog.current?.querySelectorAll<HTMLButtonElement>('button:not([disabled])')
    if(!controls?.length){event.preventDefault();return}
    const first=controls[0],last=controls[controls.length-1]
    if(event.shiftKey&&document.activeElement===first){event.preventDefault();last.focus()}
    else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first.focus()}
   }
  }
  document.addEventListener('keydown',keydown)
  return()=>{document.removeEventListener('keydown',keydown);reviewTrigger.current?.focus()}
 },[review])

 async function references(){
  const [w,s,p]=await Promise.all([api<Work[]>('/work-orders'),api<Source[]>('/material-returns/sources'),api<Policy>('/material-returns/policy')])
  setWorks(w);setSources(s);setPolicy(p);setLoaded(true);onConnection(true)
 }
 async function list(q=applied){setRows(await api<MaterialReturn[]>('/material-returns?q='+encodeURIComponent(q)))}
 function accept(row:MaterialReturn){setSelected(row);setDraft(headerOf(row));setPolicy(row.policy);if(row.posting_state==='POSTED')attempts.current.delete(row.id)}
 async function refresh(id:string){const row=await api<MaterialReturn>('/material-returns/'+id);accept(row);await list();return row}
 async function open(id:string){
  setBusy(true);setOpenError(null);setListFeedback({});setFeedback({});setLineFeedback({});setLineActionId(null);setPostFeedback({});setEditor(null)
  try{remember(id);await refresh(id);setEditing(false)}
  catch(e){const message=(e as Error).message;setOpenError({id,message});if(!rows.some(r=>r.id===id))setListFeedback({error:message})}finally{setBusy(false)}
 }
 useEffect(()=>{(async()=>{try{await references();await list('');const id=recordFromHash();if(id)await open(id)}catch(e){setListFeedback({error:(e as Error).message});onConnection(false)}})()},[])
 function newReturn(){setSelected(null);setDraft(blankHeader());setEditing(true);setEditor(null);setFeedback({});setLineFeedback({});setPostFeedback({});setHeaderKey(crypto.randomUUID());remember()}
 function cancelHeader(){setDraft(selected?headerOf(selected):blankHeader());setEditing(false);setFeedback({notice:'已取消未儲存的表頭變更；未送出異動請求。'})}
 async function saveHeader(event:FormEvent){
  event.preventDefault();setBusy(true);setFeedback({})
  try{
   const work=works.find(w=>w.id===draft.work_order_id)
   if(!work)throw Error('請明確選取製造工單。')
   const body={...draft,note:draft.note||null,work_order_version:work.row_version,...(selected?{row_version:selected.row_version}:{request_key:headerKey})}
   const row=await api<MaterialReturn>('/material-returns'+(selected?'/'+selected.id:''),{method:selected?'PUT':'POST',body:JSON.stringify(body)})
   attempts.current.delete(row.id);remember(row.id);await refresh(row.id);setEditing(false);setFeedback({notice:'退料草稿已儲存；實體庫存未異動。'})
  }catch(e){setFeedback({error:(e as Error).message})}finally{setBusy(false)}
 }
 async function removeHeader(){
  if(!selected||!confirm('確定刪除此退料草稿及其未過帳明細？'))return
  setBusy(true);setFeedback({})
  try{await api('/material-returns/'+selected.id+'?row_version='+selected.row_version,{method:'DELETE'});setSelected(null);setDraft(blankHeader());setEditor(null);setEditing(false);remember();await list();setFeedback({notice:'退料草稿已刪除；實體庫存未異動。'})}
  catch(e){setFeedback({error:(e as Error).message})}finally{setBusy(false)}
 }
 function editLine(line?:Line){
  setLineActionId(null);setLineFeedback({});setEditor({id:line?.id||null,key:crypto.randomUUID(),issue_id:line?.source.issue_id||'',draft:line?{origin_line_id:line.origin_line_id,quantity:line.quantity,note:line.note||''}:{origin_line_id:'',quantity:'',note:''}})
 }
 function changeLine(key:keyof LineDraft,value:string){setEditor(ed=>ed?{...ed,draft:{...ed.draft,[key]:value}}:null)}
 function cancelLine(){setEditor(null);setLineActionId(null);setLineFeedback({notice:'已取消未儲存的明細變更；未送出異動請求。'})}
 async function saveLine(event:FormEvent){
  event.preventDefault();if(!selected||!editor)return;setBusy(true);setLineFeedback({})
  try{
   const source=sources.find(s=>s.origin_line_id===editor.draft.origin_line_id)
   if(!source)throw Error('請重新讀取並明確選取已過帳領料明細。')
   const existing=selected.lines.find(l=>l.id===editor.id)
   const body={...editor.draft,note:editor.draft.note||null,origin_line_version:source.origin_line_version,document_version:selected.row_version,...(existing?{row_version:existing.row_version}:{request_key:editor.key})}
   await api('/material-returns/'+selected.id+'/lines'+(existing?'/'+existing.id:''),{method:existing?'PUT':'POST',body:JSON.stringify(body)})
   attempts.current.delete(selected.id);await refresh(selected.id);setEditor(null);setLineFeedback({notice:'退料明細已儲存；草稿不增加庫存，也不占用可退量。'})
  }catch(e){setLineFeedback({error:(e as Error).message})}finally{setBusy(false)}
 }
 async function removeLine(line:Line){
  if(!selected||!confirm('確定刪除此未過帳退料明細？'))return
  setBusy(true);setLineActionId(line.id);setLineFeedback({})
  try{await api('/material-returns/'+selected.id+'/lines/'+line.id+'?row_version='+line.row_version+'&document_version='+selected.row_version,{method:'DELETE'});attempts.current.delete(selected.id);await refresh(selected.id);setEditor(null);setLineActionId(null);setLineFeedback({notice:'退料草稿明細已刪除；實體庫存未異動。'})}
  catch(e){setLineFeedback({error:(e as Error).message})}finally{setBusy(false)}
 }
 async function reviewPosting(){
  if(!selected||reviewInFlight.current||postingInFlight.current)return
  reviewInFlight.current=true;setBusy(true);setPostFeedback({});setReviewFeedback({})
  try{
   const pending=attempts.current.get(selected.id)
   if(pending){reviewBody.current=pending.body;setAttempted(true);setReview(pending.review);setReviewFeedback({notice:'保留上次原請求以安全重試；尚未送出新的過帳。也可取消後重新讀取單據核對實際狀態。'});return}
   const fresh=await api<Review>('/material-returns/'+selected.id+'/posting-review')
   accept(fresh.material_return)
   if(fresh.material_return.posting_id){setPostFeedback({notice:'這張退料單已有過帳紀錄；請查看實際正向分錄。'});return}
   reviewBody.current=fresh.confirmation_token?{request_key:crypto.randomUUID(),row_version:fresh.material_return.row_version,work_order_version:fresh.material_return.work_order_version,confirmation_token:fresh.confirmation_token}:null
   setAttempted(false);setReview(fresh)
  }catch(e){setPostFeedback({error:(e as Error).message})}finally{reviewInFlight.current=false;setBusy(false)}
 }
 function closeReview(){
  if(postingInFlight.current)return
  setReview(null);setReviewFeedback({})
  setPostFeedback({notice:attempted?'已關閉確認畫面；上次請求可能已提交，請重新讀取核對結果，或開啟原請求重試。':'已取消退料過帳檢視；未送出異動請求。'})
 }
 async function confirmPosting(){
  const body=reviewBody.current
  if(!review||!body||!review.material_return.policy.posting_allowed||!policy?.posting_allowed||postingInFlight.current)return
  postingInFlight.current=true;setBusy(true);setAttempted(true);setReviewFeedback({})
  attempts.current.set(review.material_return.id,{review,body})
  try{
   const row=await api<MaterialReturn>('/material-returns/'+review.material_return.id+'/post',{method:'POST',body:JSON.stringify(body)})
   accept(row);setReview(null);setPostFeedback({notice:'退料已實際過帳，正向庫存分錄已提交。'+(row.authorization_basis==='CONTROLLED_QA_ONLY'?'這是隔離 QA 結果。':'本機展示人工確認已完成；實際分錄與來源紀錄已保留。')})
   try{await list();await references()}catch(e){setSourceFeedback({error:'退料已過帳，但來源重新讀取失敗：'+(e as Error).message})}
  }catch(e){setReviewFeedback({error:(e as Error).message+' 尚未取得成功回應；請保留原請求重試，或取消後重新讀取核對實際狀態。'})}finally{postingInFlight.current=false;setBusy(false)}
 }
 async function checkPolicy(){setBusy(true);setPostFeedback({});try{const p=await api<Policy>('/material-returns/policy');setPolicy(p);setPostFeedback({notice:p.message})}catch(e){setPostFeedback({error:(e as Error).message})}finally{setBusy(false)}}
 async function reloadSources(){
  setBusy(true);setSourceFeedback({})
  try{await references();if(selected){await refresh(selected.id);if(editing)setDraft(draft)}setSourceFeedback({notice:'已重新讀取已過帳來源、累計可退量與實體庫存；未送出異動請求。'})}
  catch(e){setSourceFeedback({error:(e as Error).message})}finally{setBusy(false)}
 }
 const eligible=sources.filter(s=>s.work_order_id===selected?.work_order_id)
 const issues=eligible.filter((s,i,all)=>all.findIndex(other=>other.issue_id===s.issue_id)===i)
 const chosen=editor?sources.find(s=>s.origin_line_id===editor.draft.origin_line_id):null
 function lineEditor(){
  if(!editor)return null
  return <form className="return-line-editor" aria-label={editor.id?'編輯退料明細':'新增退料明細'} onSubmit={saveLine}>
   <div className="form-body po-fields">
    <label htmlFor="mr-source-issue">來源已過帳領料單 *<select aria-label="來源已過帳領料單 *" id="mr-source-issue" required value={editor.issue_id} onChange={e=>setEditor({...editor,issue_id:e.target.value,draft:{...editor.draft,origin_line_id:''}})}><option value="">請明確選取已過帳領料單</option>{issues.map(s=><option key={s.issue_id} value={s.issue_id}>{s.issue_no} · {s.work_order_no} · 已過帳</option>)}</select></label>
    <label htmlFor="mr-source-line">原領料明細 *<select aria-label="原領料明細 *" id="mr-source-line" required value={editor.draft.origin_line_id} onChange={e=>changeLine('origin_line_id',e.target.value)}><option value="">請明確選取原領料行</option>{eligible.filter(s=>s.issue_id===editor.issue_id).map(s=><option key={s.origin_line_id} value={s.origin_line_id}>{s.issue_no} 第 {s.issue_line_no} 行 · {s.position.material_code} · {s.position.warehouse_code} · {s.position.lot_code||'無批次'} · 可退 {s.returnable_qty_base} {s.position.unit_code}</option>)}</select></label>
    <label htmlFor="mr-quantity">本次退料量 *<input aria-label="本次退料量 *" id="mr-quantity" type="text" required value={editor.draft.quantity} onChange={e=>changeLine('quantity',e.target.value)}/></label>
    <label htmlFor="mr-line-note">明細備註<input aria-label="明細備註" id="mr-line-note" value={editor.draft.note} onChange={e=>changeLine('note',e.target.value)}/></label>
    {chosen&&<div className="return-source-summary"><strong>{chosen.position.material_code} · {chosen.position.material_name}</strong><p>原倉庫：{chosen.position.warehouse_code} · {chosen.position.warehouse_name}／批次：{chosen.position.lot_code||'無批次'}。只退回原庫存位置。</p><p>原實際領料 {chosen.issued_qty_base}／已過帳退回 {chosen.returned_qty_base}／目前可退 {chosen.returnable_qty_base} {chosen.position.unit_code}。</p><p>本次草稿 {editor.draft.quantity||'尚未輸入'}／目前實體庫存 {chosen.position.physical_qty_base} {chosen.position.unit_code}。草稿不占用可退量；過帳時合計同一原領料行重新檢查。</p></div>}
    {!eligible.length&&<p>這張工單尚無可辨識的已過帳領料來源，不能新增無來源退料。</p>}
   </div>
   <div className="form-actions" data-feedback-group><ActionFeedback {...lineFeedback}/><div className="button-row"><button type="submit" className="button primary" disabled={busy}>儲存退料明細</button>{editor.id&&<button type="button" className="button danger" disabled={busy} onClick={()=>{const line=selected?.lines.find(l=>l.id===editor.id);if(line)void removeLine(line)}}>刪除此退料明細</button>}<button type="button" className="button secondary" disabled={busy} onClick={cancelLine}>取消</button></div></div>
  </form>
 }
 return <div className="return-page">
  <div className="page-heading"><div><div className="eyebrow">MANUFACTURING / MATERIAL RETURN</div><h1>生產退料管理</h1><p>依實際已過帳領料行退回原倉、原批與原庫存位置。儲存草稿不增加庫存。</p></div><button type="button" className="button primary" disabled={busy||!loaded} onClick={newReturn}>＋ 新增退料單</button></div>
  <div className="content-grid">
   <section className="panel list-panel" aria-label="退料單清單"><div className="panel-title"><h2>退料單清單 <span className="count">{rows.length}</span></h2><span>草稿與實際入庫分開</span></div>
    <div className="search-area" data-feedback-group><form className="search-bar" onSubmit={async e=>{e.preventDefault();setApplied(query);setListFeedback({});try{await list(query)}catch(error){setListFeedback({error:(error as Error).message})}}}><label className="sr-only" htmlFor="mr-search">搜尋退料單或工單</label><input id="mr-search" value={query} onChange={e=>setQuery(e.target.value)} placeholder="搜尋退料單號、工單或備註"/><button type="submit">搜尋</button></form><ActionFeedback {...listFeedback}/></div>
    <div className="table-wrap"><table><thead><tr><th>退料單／工單</th><th>實際狀態</th></tr></thead><tbody>{rows.map(row=><tr key={row.id} className={selected?.id===row.id?'current-row':''}><td data-feedback-group><button type="button" className="record-link" disabled={busy} aria-label={'開啟退料單 '+row.document_no} onClick={()=>open(row.id)}><strong>{row.document_no}</strong><span>{row.work_order.document_no} · {row.work_order.product_code}</span></button><ActionFeedback error={openError?.id===row.id?openError.message:''}/></td><td>{row.posting_state==='POSTED'?'已過帳':row.posting_id?'過帳紀錄需檢查':'草稿／未入庫'}<br/>{row.lines.length} 筆明細</td></tr>)}</tbody></table></div>
    {!rows.length&&<div className="empty-state"><h3>目前沒有符合的退料單</h3><p>可先新增草稿並逐筆選取已過帳領料來源。</p></div>}
   </section>
   <section className="panel editor-panel" aria-label="退料單資料"><div className="panel-title"><h2>{editing?(selected?'編輯退料表頭':'新增退料表頭'):'退料單資料'}</h2><span>{posted?'POSTED · 不可變歷史':locked?'過帳紀錄需檢查':'DRAFT'}</span></div>
    {editing?<form onSubmit={saveHeader}><div className="form-body">
     <label htmlFor="mr-number">退料單號 *</label><input id="mr-number" required value={draft.document_no} onChange={e=>setDraft({...draft,document_no:e.target.value})}/>
     <label htmlFor="mr-work">製造工單 *</label><select id="mr-work" required disabled={!!selected?.lines.length} value={draft.work_order_id} onChange={e=>setDraft({...draft,work_order_id:e.target.value})}><option value="">請明確選取製造工單</option>{works.map(w=><option key={w.id} value={w.id}>{w.document_no} · {w.product_code} · {w.product_name} · {w.approval_state}</option>)}</select>
     {!!selected?.lines.length&&<p className="field-note">已有明細，不可改派工單。來源領料行決定原料、原倉與批次。</p>}
     <label htmlFor="mr-date">退料日期 *</label><input id="mr-date" type="date" required value={draft.business_date} onChange={e=>setDraft({...draft,business_date:e.target.value})}/>
     <label htmlFor="mr-note">備註</label><textarea id="mr-note" value={draft.note} onChange={e=>setDraft({...draft,note:e.target.value})}/>
    </div><div className="form-actions" data-feedback-group><ActionFeedback {...feedback}/><div className="button-row"><button type="submit" className="button primary" disabled={busy}>{selected?'儲存退料表頭變更':'儲存退料草稿'}</button>{selected&&<button type="button" className="button danger" disabled={busy} onClick={removeHeader}>刪除退料草稿</button>}<button type="button" className="button secondary" disabled={busy} onClick={cancelHeader}>取消</button></div></div></form>:<>
     <div className="form-body">{selected?<dl className="po-header-summary"><dt>退料單號</dt><dd data-testid="mr-number-value">{selected.document_no}</dd><dt>製造工單／成品</dt><dd>{selected.work_order.document_no}<br/>{selected.work_order.product_code} · {selected.work_order.product_name}</dd><dt>退料日期</dt><dd>{selected.business_date}</dd><dt>備註</dt><dd>{selected.note||'—'}</dd><dt>實際狀態</dt><dd data-testid="mr-state">{posted?'已過帳／實際庫存已增加':locked?'過帳紀錄尚未完成，請保留資料並檢查':'草稿／未退料過帳，庫存未異動'}</dd>{posted&&<><dt>授權範圍</dt><dd>{selected.authorization_basis} · {selected.authorization_basis==='CONTROLLED_QA_ONLY'?'隔離 QA，不代表 Owner 實際退料核准':'本機展示，不代表正式電子簽核'}</dd><dt>過帳時間</dt><dd>{selected.posted_at}</dd></>}</dl>:<p>選取退料單或新增草稿，來源必須是同一工單的實際已過帳領料行。</p>}</div>
     <div className="form-actions" data-feedback-group><ActionFeedback {...feedback}/>{selected&&!locked&&<div className="button-row"><button type="button" className="button primary" disabled={busy||!!editor} onClick={()=>{setDraft(headerOf(selected));setEditing(true);setFeedback({})}}>編輯退料表頭</button><button type="button" className="button danger" disabled={busy||!!editor} onClick={removeHeader}>刪除退料草稿</button></div>}</div>
    </>}
   </section>
  </div>
  {selected&&<section className="panel" aria-label="退料明細"><div className="panel-title"><h2>逐筆退料明細</h2><span>一次編輯並儲存一筆明細</span>{!locked&&!editing&&<button type="button" className="button secondary" disabled={busy||!!editor} onClick={()=>editLine()}>＋ 新增退料明細</button>}</div>
   <div className="table-wrap"><table className="return-lines"><thead><tr><th>原領料來源／原料</th><th>原倉庫／批次</th><th>原領料／已退／目前可退</th><th>本次退料／操作</th></tr></thead><tbody>{selected.lines.map(line=><ReturnLine key={line.id} line={line} busy={busy} locked={locked||editing} editor={editor} feedback={lineActionId===line.id?lineFeedback:{}} onEdit={()=>editLine(line)} onDelete={()=>removeLine(line)} renderEditor={lineEditor}/>)}</tbody></table></div>
   {editor&&!editor.id?lineEditor():!editor&&<div className="form-actions" data-feedback-group><ActionFeedback {...(!lineActionId?lineFeedback:{})}/></div>}
   {!selected.lines.length&&!editor&&<div className="empty-state"><p>尚無退料明細。每筆明細必須明確選取原領料行。</p></div>}
  </section>}
  <section className="panel" aria-label="退料過帳政策"><div className="panel-title"><h2>退料過帳與獨立授權</h2><span data-testid="mr-policy">{policy?.posting_allowed?(policy.authorization_basis==='LOCAL_DEMO_MANUAL'?'本機展示 · 人工確認退料':'已開啟此環境專用授權'):'CLOSED · 此環境未啟用實際退料'}</span></div>
   <div className="form-body"><p className="return-demo-disclosure">{DISCLOSURE}</p><p>{policy?.message||'正在讀取後端獨立退料授權政策…'}</p><p>本機展示的實際退料須先檢視來源與庫存，再明確確認。一般執行環境不開放過帳。檢視內容、取消及 Escape 只讀取資料。</p><p>正式電子簽核、身分權限及未核定 D01–D16 政策仍待決定。MRP 仍為初步毛需求減實體庫存試算，尚未依已領／已退量計算工單剩餘需求。</p>
    {posted&&<div data-testid="mr-movements"><p>已過帳表頭、明細與原領料關聯不可修改或刪除；原領料負向分錄永久保留。</p>{selected.movements.map(m=>{const line=selected.lines.find(l=>l.id===m.return_line_id);return <p key={m.id}>實際正向庫存分錄：{positive(m.qty_base_delta)} {line?.source.position.unit_code} · 退料明細 {line?.line_no}<br/>{line?.source.issue_no} 第 {line?.source.issue_line_no} 行 · {line?.source.position.material_code}／{line?.source.position.warehouse_code}／{line?.source.position.lot_code||'無批次'}<br/>目前實體庫存 {line?.source.position.physical_qty_base} {line?.source.position.unit_code}</p>})}</div>}
   </div>
   <div className="form-actions" data-feedback-group><ActionFeedback {...postFeedback}/><div className="button-row">{selected&&!locked&&<button ref={reviewTrigger} type="button" className="button primary" disabled={busy||editing||!!editor} onClick={reviewPosting}>檢視退料過帳</button>}{!policy?.posting_allowed&&<button type="button" className="button secondary" disabled>實際退料過帳（尚未授權）</button>}<button type="button" className="button secondary" disabled={busy} onClick={checkPolicy}>檢查退料過帳政策</button></div></div>
  </section>
  <div className="form-actions" data-feedback-group><ActionFeedback {...sourceFeedback}/><button type="button" className="button secondary" disabled={busy} onClick={reloadSources}>重新讀取領料來源與庫存</button></div>
  {selected&&<details className="panel form-body"><summary>退料單系統稽核</summary><p>退料單：{selected.id} · 資料版次 {selected.row_version}<br/>工單來源版次 {selected.work_order_version}<br/>建立：{selected.created_at} · {selected.created_by}<br/>更新：{selected.updated_at}<br/>實際過帳操作員：{selected.posted_by||'尚未過帳'}<br/>過帳識別：{selected.posting_id||'尚未過帳'}</p></details>}
  {review&&<div className="modal-backdrop return-confirm-backdrop"><section ref={reviewDialog} className="modal return-post-dialog" role="dialog" aria-modal="true" aria-labelledby="mr-review-title" aria-describedby="mr-review-description" tabIndex={-1}>
   <div className="panel-title"><h2 id="mr-review-title">檢視退料過帳</h2><span>{attempted?'保留原請求以安全核對／重試':review.material_return.policy.posting_allowed?'專用授權 · 最後確認前不入庫':'CLOSED · 唯讀檢視'}</span></div>
   <div className="modal-body"><p id="mr-review-description">核對實際來源與正向回庫量。只有最後「確認並增加庫存」會送出過帳請求，仍須通過伺服器與資料庫的獨立退料授權。</p><p className="return-demo-disclosure">{DISCLOSURE}</p><p>{review.material_return.policy.message}</p>
    <dl className="po-header-summary"><dt>退料單</dt><dd>{review.material_return.document_no}</dd><dt>製造工單／成品</dt><dd>{review.material_return.work_order.document_no} · {review.material_return.work_order.product_code} · {review.material_return.work_order.product_name}</dd><dt>退料日期</dt><dd>{review.material_return.business_date}</dd></dl>
    <div className="table-wrap"><table><thead><tr><th>原領料／原料／原位置</th><th>原領料／已退／可退</th><th>本次退料／正向分錄</th><th>目前實體庫存</th><th>預期過帳後庫存</th></tr></thead><tbody>{review.material_return.lines.map(line=>{const s=line.source,p=s.position,m=review.expected_movements.find(m=>m.return_line_id===line.id);return <tr key={line.id}><td>{s.issue_no} 第 {s.issue_line_no} 行<br/>{p.material_code} · {p.material_name}<br/>{p.warehouse_code} · {p.warehouse_name}<br/>{p.lot_code||'無批次（原庫存位置）'}</td><td>原領 {s.issued_qty_base}<br/>已退 {s.returned_qty_base}<br/>可退 {s.returnable_qty_base} {p.unit_code}</td><td>本行 {line.quantity} {p.unit_code}<br/>同源合計 {line.proposed_qty_base}<br/>本單後可退 {line.remaining_after_proposal_qty_base}<br/>分錄 {m?positive(m.qty_base_delta)+' '+m.unit_code:'未提供，不能確認過帳'}</td><td>{m?.current_stock_qty_base??p.physical_qty_base} {p.unit_code}</td><td>{m?.expected_stock_qty_base??'未提供'} {p.unit_code}</td></tr>})}</tbody></table></div>
    <p>同一原領料行的所有退料明細會合計檢查可退量；草稿不列入已退量。同一庫存位置的預期庫存是全單合計後的最終值，不能逐行再重複加總。</p><p>只增加原領料位置的庫存，原領料負向分錄保持不變。{review.material_return.policy.authorization_basis==='CONTROLLED_QA_ONLY'?'這是隔離 QA 過帳。':'每次實際退料仍須人工確認；本機展示不代表正式電子簽核。'}</p>
   </div>
   <div className="form-actions" data-feedback-group><ActionFeedback {...reviewFeedback}/><div className="button-row"><button type="button" className="button primary" disabled={busy||!policy?.posting_allowed||!review.material_return.policy.posting_allowed||!review.confirmation_token||!review.material_return.lines.length||review.material_return.lines.some(l=>!review.expected_movements.some(m=>m.return_line_id===l.id))} onClick={confirmPosting}>{busy?'過帳處理中…':attempted?'原請求重試並核對結果':'確認並增加庫存'}</button><button ref={reviewCancel} type="button" className="button secondary" disabled={busy} onClick={closeReview}>取消</button></div></div>
  </section></div>}
 </div>
}

function ReturnLine({line,busy,locked,editor,feedback,onEdit,onDelete,renderEditor}:{line:Line;busy:boolean;locked:boolean;editor:Editor|null;feedback:Feedback;onEdit:()=>void;onDelete:()=>void;renderEditor:()=>ReactNode}){
 const s=line.source,p=s.position,expanded=editor?.id===line.id
 return <><tr data-testid={'mr-line-'+line.line_no}><td>{s.issue_no} 第 {s.issue_line_no} 行<br/>{p.material_code} · {p.material_name}<br/>{line.note||'—'}</td><td>{p.warehouse_code} · {p.warehouse_name}<br/>{p.lot_code||'無批次（原庫存位置）'}</td><td>原領 {s.issued_qty_base}<br/>已退 {s.returned_qty_base}<br/>可退 {s.returnable_qty_base} {p.unit_code}</td><td><strong>本次 {line.quantity} {p.unit_code}</strong><p>本單同源合計 {line.proposed_qty_base}<br/>本單後可退 {line.remaining_after_proposal_qty_base} {p.unit_code}</p>{!locked&&<div className="button-row" data-feedback-group><ActionFeedback {...feedback}/><button type="button" className="button secondary" disabled={busy||!!editor} aria-label={'展開編輯退料明細 '+line.line_no} aria-expanded={expanded} onClick={onEdit}>展開編輯</button><button type="button" className="button danger" disabled={busy||!!editor} aria-label={'刪除退料明細 '+line.line_no} onClick={onDelete}>刪除</button></div>}<details><summary>原領料過帳與來源稽核</summary><p>原領料過帳：{s.issue_posting_id}<br/>原過帳時間：{s.issue_posted_at}<br/>原領料行：{line.origin_line_id} · 版次 {line.origin_line_version}<br/>原庫存位置：{p.id}<br/>退料行：{line.id} · 版次 {line.row_version}<br/>目前實體庫存：{p.physical_qty_base} {p.unit_code}</p></details></td></tr>{expanded&&<tr className="return-expanded-row"><td colSpan={4}>{renderEditor()}</td></tr>}</>
}
