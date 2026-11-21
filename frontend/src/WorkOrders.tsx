import {DocumentApproval} from './DocumentApproval'
import { useEffect, useState, type FormEvent } from 'react'
import { api } from './api'
import { ActionFeedback } from './ActionFeedback'

type Item={id:string;code:string;name:string;base_unit_id:string;is_active:boolean}
type Unit={id:string;code:string;name:string}
type Source={id:string;product_id:string;revision:string;release_state:string;is_active:boolean;row_version:number}
type Draft={document_no:string;product_id:string;planned_qty_base:string;planned_start:string;planned_finish:string;note:string;
  bom_id:string;routing_id:string;bom_version:number|null;routing_version:number|null}
type WorkOrder={id:string;document_no:string;product_id:string;planned_qty_base:string;planned_start:string;planned_finish:string;
  note:string|null;bom_id:string|null;routing_id:string|null;approval_state:string;row_version:number;created_at:string;updated_at:string;created_by:string;
  product_code:string;product_name:string;product_specification:string|null;product_base_unit_id:string;product_base_unit_code:string;product_base_unit_name:string;
  bom:Source|null;routing:Source|null;date_order_warning:string|null}
type Feedback={error?:string;notice?:string}
const blank=():Draft=>({document_no:'',product_id:'',planned_qty_base:'',planned_start:'',planned_finish:'',note:'',bom_id:'',routing_id:'',bom_version:null,routing_version:null})
const fromRecord=(r:WorkOrder):Draft=>({document_no:r.document_no,product_id:r.product_id,planned_qty_base:r.planned_qty_base,
  planned_start:r.planned_start,planned_finish:r.planned_finish,note:r.note??'',bom_id:r.bom_id??'',routing_id:r.routing_id??'',
  bom_version:r.bom?.row_version??null,routing_version:r.routing?.row_version??null})
const sourceLabel=(s:Source)=>s.revision+' · '+s.release_state+' · '+(s.is_active?'啟用':'停用')+' · 資料版次 '+s.row_version

export function WorkOrders({onConnection}:{onConnection:(online:boolean)=>void}){
  const [rows,setRows]=useState<WorkOrder[]>([]),[selected,setSelected]=useState<WorkOrder|null>(null)
  const [products,setProducts]=useState<Item[]>([]),[units,setUnits]=useState<Unit[]>([]),[boms,setBoms]=useState<Source[]>([]),[routings,setRoutings]=useState<Source[]>([])
  const [draft,setDraft]=useState<Draft>(blank),[editing,setEditing]=useState(true),[requestKey,setRequestKey]=useState(()=>crypto.randomUUID())
  const [query,setQuery]=useState(''),[applied,setApplied]=useState(''),[busy,setBusy]=useState(false),[refsBusy,setRefsBusy]=useState(false)
  const [feedback,setFeedback]=useState<Feedback>({}),[sourceFeedback,setSourceFeedback]=useState<Feedback>({})
  const [listError,setListError]=useState(''),[openError,setOpenError]=useState<{id:string;message:string}|null>(null)
  async function loadList(q=applied){setRows(await api<WorkOrder[]>('/work-orders?q='+encodeURIComponent(q)));setListError('');onConnection(true)}
  async function loadRefs(){
    const [p,u,b,r]=await Promise.all([api<Item[]>('/finished-goods'),api<Unit[]>('/units'),api<Source[]>('/boms'),api<Source[]>('/routings')])
    setProducts(p);setUnits(u);setBoms(b);setRoutings(r)
  }
  function recordHash(id?:string){window.history.replaceState(null,'',id?'#work-orders?record='+encodeURIComponent(id):'#work-orders')}
  function clearFeedback(){setFeedback({});setSourceFeedback({});setOpenError(null)}
  function newOrder(){setSelected(null);setDraft(blank());setRequestKey(crypto.randomUUID());setEditing(true);clearFeedback();recordHash()}
  async function open(id:string,target:'list'|'header'='list',refreshRefs=true){
    clearFeedback()
    try{
      const [r]=await Promise.all([api<WorkOrder>('/work-orders/'+id),refreshRefs?loadRefs():Promise.resolve()])
      setSelected(r);setDraft(fromRecord(r));setEditing(false);recordHash(r.id)
    }catch(e){if(target==='header')setFeedback({error:(e as Error).message});else setOpenError({id,message:(e as Error).message})}
  }
  useEffect(()=>{
    const syncRecord=()=>{
      if(window.location.hash.slice(1).split('?')[0]!=='work-orders')return
      const id=new URLSearchParams(window.location.hash.split('?')[1]||'').get('record')
      if(id)void open(id,'header');else newOrder()
    }
    Promise.all([loadList(''),loadRefs()]).then(()=>{
      const id=new URLSearchParams(window.location.hash.split('?')[1]||'').get('record')
      if(id)return open(id,'header',false)
    }).catch(e=>{setListError(e.message);onConnection(false)})
    window.addEventListener('hashchange',syncRecord);return()=>window.removeEventListener('hashchange',syncRecord)
  },[])
  function change<K extends keyof Draft>(key:K,value:Draft[K]){setDraft(d=>({...d,[key]:value}))}
  function chooseSource(kind:'bom'|'routing',id:string){
    const row=(kind==='bom'?boms:routings).find(s=>s.id===id)
    setDraft(d=>kind==='bom'?{...d,bom_id:id,bom_version:row?.row_version??null}:{...d,routing_id:id,routing_version:row?.row_version??null})
    setSourceFeedback({})
  }
  function sourceProblem(kind:'bom'|'routing'){
    const id=kind==='bom'?draft.bom_id:draft.routing_id;if(!id)return ''
    const row=(kind==='bom'?boms:routings).find(s=>s.id===id),label=kind==='bom'?'BOM':'途程'
    if(!row)return '原選'+label+'已無法讀取，請明確清除或重新選取。'
    if(row.product_id!==draft.product_id)return '原選'+label+'不屬於目前成品，請明確清除或重新選取；系統不會自動替換。'
    if(row.row_version!==(kind==='bom'?draft.bom_version:draft.routing_version))return '原選'+label+'的資料版次已變更，請先選「未指定」再重新選取。'
    return ''
  }
  async function refreshSources(){
    setRefsBusy(true);setSourceFeedback({})
    try{await loadRefs();setSourceFeedback({notice:'來源清單已更新；原選內容保留，未自動替換。若資料版次改變，請明確重新選取。'})}
    catch(e){setSourceFeedback({error:(e as Error).message})}finally{setRefsBusy(false)}
  }
  function cancel(){
    if(selected){setDraft(fromRecord(selected));setEditing(false);clearFeedback()}
    else newOrder()
  }
  async function search(e:FormEvent){e.preventDefault();setApplied(query);try{await loadList(query)}catch(error){setListError((error as Error).message)}}
  async function save(e:FormEvent){
    e.preventDefault();setFeedback({})
    const problem=sourceProblem('bom')||sourceProblem('routing')
    if(problem){setFeedback({error:problem});return}
    setBusy(true)
    const body={...draft,note:draft.note||null,bom_id:draft.bom_id||null,routing_id:draft.routing_id||null,
      bom_version:draft.bom_id?draft.bom_version:null,routing_version:draft.routing_id?draft.routing_version:null,
      ...(selected?{row_version:selected.row_version}:{request_key:requestKey})}
    try{
      const r=await api<WorkOrder>('/work-orders'+(selected?'/'+selected.id:''),{method:selected?'PUT':'POST',body:JSON.stringify(body)})
      setSelected(r);setDraft(fromRecord(r));setEditing(false);recordHash(r.id);await loadList()
      setFeedback({notice:selected?'工單變更已儲存。':'工單已新增；僅保存草稿，沒有發放或庫存異動。'})
    }catch(error){setFeedback({error:(error as Error).message})}finally{setBusy(false)}
  }
  async function remove(){
    if(!selected||!window.confirm('確定刪除草稿工單「'+selected.document_no+'」？已被引用的資料將受到保護，成品、BOM 與途程不會一併刪除。'))return
    setBusy(true);setFeedback({})
    try{await api('/work-orders/'+selected.id+'?row_version='+selected.row_version,{method:'DELETE'});newOrder();await loadList();setFeedback({notice:'草稿工單已刪除。'})}
    catch(error){setFeedback({error:(error as Error).message})}finally{setBusy(false)}
  }
  const unit=units.find(u=>u.id===products.find(p=>p.id===draft.product_id)?.base_unit_id)
  const dateWarning=draft.planned_start&&draft.planned_finish&&draft.planned_finish<draft.planned_start?'預計完工日早於開工日；草稿可保留，日期排序政策 PENDING，不代表已核准排程。':''
  const sourceSelect=(kind:'bom'|'routing')=>{
    const id=kind==='bom'?draft.bom_id:draft.routing_id,all=kind==='bom'?boms:routings,label=kind==='bom'?'BOM':'途程'
    const chosen=all.find(s=>s.id===id),invalid=!!id&&(!chosen||chosen.product_id!==draft.product_id)
    return <div>
      <label htmlFor={'wo-'+kind}>{label} 版本（草稿關聯，可留白）</label>
      <select id={'wo-'+kind} disabled={!draft.product_id||busy||refsBusy} value={id} onChange={e=>chooseSource(kind,e.target.value)}>
        <option value="">未指定（保留不完整草稿）</option>
        {invalid&&<option value={id} disabled>原選 {chosen?.revision||label} 與目前成品不符或已無法讀取，請清除／重選</option>}
        {all.filter(s=>s.product_id===draft.product_id).map(s=><option key={s.id} value={s.id}>{sourceLabel(s)}</option>)}
      </select>
      <p className="field-note" data-testid={'wo-'+kind+'-selection'}>{chosen?sourceLabel(chosen):'未指定來源；不代表可以執行製造。'}</p>
      {sourceProblem(kind)&&<p className="field-note">{sourceProblem(kind)}</p>}
    </div>
  }
  const actions=(isEditing:boolean)=><div className="form-actions" data-feedback-group>
    {selected&&<DocumentApproval key={selected.id} type="WORK_ORDER" id={selected.id} version={selected.row_version} blocked={busy||isEditing}/>}
    <ActionFeedback {...feedback} onReload={selected?()=>{void open(selected.id,'header')}:undefined} busy={busy}/>
    {isEditing?<button className="button primary" type="submit" disabled={busy||refsBusy}>{selected?'儲存變更':'儲存工單'}</button>:<button className="button primary" type="button" disabled={busy} onClick={()=>{setDraft(fromRecord(selected!));setEditing(true);clearFeedback()}}>編輯工單</button>}
    {selected&&<button className="button danger" type="button" disabled={busy} onClick={remove}>刪除工單</button>}
    {isEditing&&<button className="button" type="button" disabled={busy} onClick={cancel}>取消</button>}
    {selected&&<button className="button" type="button" disabled={busy} onClick={()=>{void open(selected.id,'header')}}>重新讀取工單</button>}
  </div>
  const sourceSummary=(kind:'bom'|'routing')=>{
    const source=kind==='bom'?selected?.bom:selected?.routing,label=kind==='bom'?'BOM':'途程'
    return <dd data-testid={'wo-'+kind+'-value'}>{source?<>{sourceLabel(source)} <a href={(kind==='bom'?'#boms':'#routings')+'?record='+source.id}>查看{label} ↗</a></>:'未指定（不完整草稿）'}</dd>
  }
  return <>
    <div className="page-heading"><div><div className="eyebrow">MANUFACTURING / DRAFT WORK ORDERS</div><h1>製造工單管理</h1><p>分別保存成品工單草稿；選取來源不代表已核准、發放或凍結製造標準。</p></div><button className="button primary new-button" disabled={busy} onClick={newOrder}>＋ 新增工單</button></div>
    <div className="page-meta"><span><span className="teal-dot"/>製造生產</span><span>DRAFT 草稿維護</span><span className="meta-right">發放、快照與執行政策 PENDING</span></div>
    <div className="po-layout">
      <section className="panel po-list" aria-label="製造工單清單"><div className="panel-title"><h2>工單清單 <span className="count">{rows.length}</span></h2></div>
        <div className="search-area" data-feedback-group><form className="search-bar" onSubmit={search}><label className="sr-only" htmlFor="wo-search">搜尋製造工單</label><input id="wo-search" value={query} onChange={e=>setQuery(e.target.value)} placeholder="搜尋工單、成品或備註"/><button type="submit">搜尋</button></form><ActionFeedback error={listError}/></div>
        {rows.map(r=><div className={'po-list-row '+(selected?.id===r.id?'current-row':'')} key={r.id} data-feedback-group><button className="record-link" disabled={busy} onClick={()=>{void open(r.id)}} aria-label={'開啟工單 '+r.document_no}><span>{r.document_no} · {r.approval_state}</span><strong>{r.product_code} · {r.product_name}</strong><span>{r.planned_qty_base} {r.product_base_unit_code} · {r.product_base_unit_name}</span><span>{r.planned_start} ～ {r.planned_finish}</span></button><ActionFeedback error={openError?.id===r.id?openError.message:''}/></div>)}
        {!rows.length&&<div className="empty-state"><h3>{applied?'沒有符合的工單':'尚無製造工單'}</h3><p>建立獨立草稿；不會自動選用 BOM 或途程。</p></div>}
      </section>
      <div className="po-detail">
        <section className="panel po-header" aria-label="製造工單資料"><div className="panel-title"><h2>{selected?selected.document_no:'新增製造工單'}</h2><span className="form-kind" data-testid="wo-status">DRAFT · 草稿，未發放</span></div>
          {editing?<form onSubmit={save} aria-label="編輯製造工單"><div className="form-body po-fields">
            <div><label htmlFor="wo-number">工單編號 *</label><input id="wo-number" required value={draft.document_no} onChange={e=>change('document_no',e.target.value)}/></div>
            <div><label htmlFor="wo-product">成品 *</label><select id="wo-product" required value={draft.product_id} onChange={e=>change('product_id',e.target.value)}><option value="">請選擇成品</option>{products.map(p=><option key={p.id} value={p.id}>{p.code} · {p.name}{p.is_active?'':'（停用）'}</option>)}</select></div>
            <div><label htmlFor="wo-qty">計畫量 *</label><input id="wo-qty" type="text" inputMode="decimal" required value={draft.planned_qty_base} onChange={e=>change('planned_qty_base',e.target.value)}/></div>
            <div><label htmlFor="wo-unit">成品基本單位</label><input id="wo-unit" readOnly value={unit?unit.code+' · '+unit.name:'請先選擇成品'}/></div>
            <p className="field-note po-wide">有號與零計畫量可保留為草稿證據；發放資格、取位與換算政策 PENDING。不執行物料需求或庫存計算。</p>
            <div><label htmlFor="wo-start">預計開工日 *</label><input id="wo-start" type="date" required value={draft.planned_start} onChange={e=>change('planned_start',e.target.value)}/></div>
            <div><label htmlFor="wo-finish">預計完工日 *</label><input id="wo-finish" type="date" required value={draft.planned_finish} onChange={e=>change('planned_finish',e.target.value)}/></div>
            {dateWarning&&<p className="field-note po-wide" data-testid="wo-date-warning">{dateWarning}</p>}
            {sourceSelect('bom')}{sourceSelect('routing')}
            <div className="po-wide"><p className="field-note">僅顯示同成品來源供明確選取；未指定不代表可以生產。DRAFT／停用狀態如實呈現，不自動選版，也沒有建立發放快照。</p><p className="field-note">引用中的 BOM／途程內容受保護；解除全部工單引用後可另行維護。製造發放快照政策仍待核定。</p><div className="po-suggestion" data-feedback-group><button className="button" type="button" disabled={busy||refsBusy} onClick={()=>{void refreshSources()}}>重新整理來源</button><ActionFeedback {...sourceFeedback}/></div></div>
            <div className="po-wide"><label htmlFor="wo-note">備註／生產說明</label><textarea id="wo-note" value={draft.note} onChange={e=>change('note',e.target.value)}/></div>
          </div>{actions(true)}</form>:selected&&<><dl className="po-header-summary"><dt>工單編號</dt><dd data-testid="wo-number-value">{selected.document_no}</dd><dt>成品</dt><dd>{selected.product_code} · {selected.product_name}</dd><dt>目前成品規格</dt><dd>{selected.product_specification||'—'}</dd><dt>計畫量</dt><dd data-testid="wo-planned-quantity">{selected.planned_qty_base} {selected.product_base_unit_code} · {selected.product_base_unit_name}</dd><dt>預計開工日</dt><dd data-testid="wo-planned-start">{selected.planned_start}</dd><dt>預計完工日</dt><dd data-testid="wo-planned-finish">{selected.planned_finish}</dd><dt>BOM 關聯</dt>{sourceSummary('bom')}<dt>途程關聯</dt>{sourceSummary('routing')}<dt>備註／生產說明</dt><dd data-testid="wo-note-value">{selected.note||'—'}</dd></dl>
            {selected.date_order_warning&&<p className="field-note po-policy" data-testid="wo-date-warning">{selected.date_order_warning}</p>}
            <p className="field-note po-policy">目前來源資料與資料版次僅供草稿關聯；未核准、未發放，也沒有製造成品／用料／步驟快照。</p><p className="field-note po-policy">引用中的 BOM／途程內容受保護；解除全部工單引用後可另行維護。製造發放快照政策仍待核定。</p>{actions(false)}
          </>}
          {selected&&<details className="audit po-audit"><summary>系統紀錄 · 工單版次 {selected.row_version}</summary><dl><dt>UUID</dt><dd data-testid="wo-uuid">{selected.id}</dd><dt>建立時間</dt><dd>{selected.created_at}</dd><dt>最後更新</dt><dd>{selected.updated_at}</dd><dt>建立者 UUID</dt><dd>{selected.created_by}</dd></dl></details>}
        </section>
      </div>
    </div>
    <footer className="page-footer">TIGER ERP <span>本機示範版 · 製造工單管理</span><span>僅保存草稿，不進行製造執行或庫存異動</span></footer>
  </>
}
