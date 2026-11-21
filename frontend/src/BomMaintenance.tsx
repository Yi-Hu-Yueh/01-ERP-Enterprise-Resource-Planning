import { useEffect, useState, type FormEvent } from 'react'
import { api } from './api'
import { ActionFeedback } from './ActionFeedback'

type Ref={id:string;code:string;name:string}
type Item=Ref&{base_unit_id:string}
type Audit={id:string;row_version:number;created_at:string;updated_at:string;created_by:string}
type HeaderDraft={product_id:string;revision:string;output_basis_qty:string;valid_from:string;valid_to:string;is_active:''|'true'|'false'}
type ComponentDraft={material_id:string;usage_qty_base:string;purpose_note:string}
type Component=Audit&{bom_id:string;line_no:number;material_id:string;usage_qty_base:string;purpose_note:string|null;material_code:string;material_name:string;material_base_unit_code:string;material_base_unit_name:string}
type Bom=Audit&{product_id:string;revision:string;output_basis_qty:string;valid_from:string;valid_to:string|null;is_active:boolean;release_state:string;product_code:string;product_name:string;product_base_unit_code:string;product_base_unit_name:string;components:Component[]}
type Feedback={error?:string;notice?:string;id?:string}
type Editor={id:string|null;key:string;draft:ComponentDraft}
const blankHeader=():HeaderDraft=>({product_id:'',revision:'',output_basis_qty:'',valid_from:'',valid_to:'',is_active:''})
const blankComponent=():ComponentDraft=>({material_id:'',usage_qty_base:'',purpose_note:''})
const headerDraft=(r:Bom):HeaderDraft=>({product_id:r.product_id,revision:r.revision,output_basis_qty:r.output_basis_qty,valid_from:r.valid_from,valid_to:r.valid_to||'',is_active:r.is_active?'true':'false'})
const componentDraft=(r:Component):ComponentDraft=>({material_id:r.material_id,usage_qty_base:r.usage_qty_base,purpose_note:r.purpose_note||''})

export function BomMaintenance({onConnection}:{onConnection:(online:boolean)=>void}){
  const [rows,setRows]=useState<Bom[]>([]),[selected,setSelected]=useState<Bom|null>(null)
  const [products,setProducts]=useState<Item[]>([]),[materials,setMaterials]=useState<Item[]>([]),[units,setUnits]=useState<Ref[]>([])
  const [draft,setDraft]=useState<HeaderDraft>(blankHeader),[headerEditing,setHeaderEditing]=useState(true)
  const [headerKey,setHeaderKey]=useState(()=>crypto.randomUUID()),[editor,setEditor]=useState<Editor|null>(null)
  const [busy,setBusy]=useState(false),[query,setQuery]=useState(''),[applied,setApplied]=useState('')
  const [headerFeedback,setHeaderFeedback]=useState<Feedback>({}),[lineFeedback,setLineFeedback]=useState<Feedback>({})
  const [listError,setListError]=useState(''),[openError,setOpenError]=useState<{id:string;message:string}|null>(null)
  async function loadList(q=applied){setRows(await api<Bom[]>('/boms?q='+encodeURIComponent(q)));setListError('');onConnection(true)}
  async function loadRefs(){const [p,m,u]=await Promise.all([api<Item[]>('/finished-goods'),api<Item[]>('/raw-materials'),api<Ref[]>('/units')]);setProducts(p);setMaterials(m);setUnits(u)}
  useEffect(()=>{Promise.all([loadList(''),loadRefs()]).then(()=>{const id=new URLSearchParams(window.location.hash.split('?')[1]||'').get('record');if(id)return open(id)}).catch(e=>{setListError(e.message);onConnection(false)})},[])
  function clearFeedback(){setHeaderFeedback({});setLineFeedback({});setOpenError(null)}
  function newBom(){setSelected(null);setDraft(blankHeader());setHeaderKey(crypto.randomUUID());setHeaderEditing(true);setEditor(null);clearFeedback()}
  async function open(id:string){
    clearFeedback()
    try{const r=await api<Bom>('/boms/'+id);await loadRefs();setSelected(r);setDraft(headerDraft(r));setHeaderEditing(false);setEditor(null)}
    catch(e){setOpenError({id,message:(e as Error).message})}
  }
  async function refresh(id:string){const r=await api<Bom>('/boms/'+id);setSelected(r);setDraft(headerDraft(r));await loadList();return r}
  async function search(e:FormEvent){e.preventDefault();setApplied(query);try{await loadList(query)}catch(e){setListError((e as Error).message)}}
  function cancelHeader(){if(selected){setDraft(headerDraft(selected));setHeaderEditing(false);clearFeedback()}else newBom()}
  async function saveHeader(e:FormEvent){
    e.preventDefault();setBusy(true);setHeaderFeedback({})
    const body={...draft,is_active:draft.is_active==='true',valid_to:draft.valid_to||null,...(selected?{row_version:selected.row_version}:{request_key:headerKey})}
    try{const r=await api<Bom>('/boms'+(selected?'/'+selected.id:''),{method:selected?'PUT':'POST',body:JSON.stringify(body)})
      setSelected(r);setDraft(headerDraft(r));setHeaderEditing(false);await loadList();setHeaderFeedback({notice:'BOM 表頭已儲存。'})
    }catch(e){setHeaderFeedback({error:(e as Error).message})}finally{setBusy(false)}
  }
  async function removeHeader(){
    if(!selected||!window.confirm('確定刪除此 BOM？仍有明細、引用或歷史封存的版本將受到保護。'))return
    setBusy(true);setHeaderFeedback({})
    try{await api('/boms/'+selected.id+'?row_version='+selected.row_version,{method:'DELETE'});newBom();await loadList();setHeaderFeedback({notice:'BOM 已刪除。'})}
    catch(e){setHeaderFeedback({error:(e as Error).message})}finally{setBusy(false)}
  }
  function editComponent(r?:Component){setEditor({id:r?.id||null,key:crypto.randomUUID(),draft:r?componentDraft(r):blankComponent()});clearFeedback()}
  function changeComponent(k:keyof ComponentDraft,v:string){setEditor(e=>e?{...e,draft:{...e.draft,[k]:v}}:null)}
  async function saveComponent(e:FormEvent){
    e.preventDefault();if(!selected||!editor)return
    setBusy(true);setLineFeedback({})
    const r=editor.id?selected.components.find(c=>c.id===editor.id):null
    const body={...editor.draft,purpose_note:editor.draft.purpose_note||null,bom_version:selected.row_version,...(r?{row_version:r.row_version}:{request_key:editor.key})}
    try{const c=await api<Component>('/boms/'+selected.id+'/components'+(r?'/'+r.id:''),{method:r?'PUT':'POST',body:JSON.stringify(body)})
      await refresh(selected.id);setEditor(null);setLineFeedback({id:c.id,notice:'用料明細已儲存。'})
    }catch(e){setLineFeedback({id:editor.id||'new',error:(e as Error).message})}finally{setBusy(false)}
  }
  async function removeComponent(r:Component){
    if(!selected||!window.confirm('確定刪除此筆未被引用的用料明細？'))return
    setBusy(true);setLineFeedback({})
    try{await api('/boms/'+selected.id+'/components/'+r.id+'?row_version='+r.row_version+'&bom_version='+selected.row_version,{method:'DELETE'})
      await refresh(selected.id);setEditor(null);setLineFeedback({id:'new',notice:'用料明細已刪除。'})
    }catch(e){setLineFeedback({id:r.id,error:(e as Error).message})}finally{setBusy(false)}
  }
  const reload=selected?()=>{void open(selected.id)}:undefined
  const sealed=!!selected&&selected.release_state!=='DRAFT'
  const basis=selected?selected.output_basis_qty+' '+selected.product_base_unit_code+' · '+selected.product_base_unit_name:''
  const headerActions=(editing:boolean)=><div className="form-actions" data-feedback-group>
    <ActionFeedback {...headerFeedback} onReload={reload} busy={busy}/>
    {editing?<button className="button primary" type="submit" disabled={busy}>儲存表頭</button>:<button className="button primary" disabled={busy||!!editor||sealed} onClick={()=>{setDraft(headerDraft(selected!));setHeaderEditing(true);clearFeedback()}}>編輯表頭</button>}
    {selected&&<button className="button danger" type="button" disabled={busy||!!editor||sealed} onClick={removeHeader}>刪除 BOM</button>}
    {editing&&<button className="button" type="button" disabled={busy} onClick={cancelHeader}>取消</button>}
  </div>
  const componentEditor=()=>{
    if(!editor||!selected)return null
    const r=editor.id?selected.components.find(c=>c.id===editor.id):null,d=editor.draft
    const unit=units.find(u=>u.id===materials.find(m=>m.id===d.material_id)?.base_unit_id)
    return <form className="po-line-editor" onSubmit={saveComponent} aria-label={r?'編輯用料明細':'新增用料明細'}>
      <div className="form-body po-fields">
        <p className="field-note po-wide">每產出 {basis} 成品所需的原料基本單位用量。</p>
        <div className="po-wide"><label htmlFor="bom-material">原物料 *</label><select id="bom-material" required value={d.material_id} onChange={e=>changeComponent('material_id',e.target.value)}><option value="">請選擇原物料</option>{materials.map(m=><option key={m.id} value={m.id}>{m.code} · {m.name}</option>)}</select></div>
        <div><label htmlFor="bom-usage">標準用量 *</label><input id="bom-usage" type="text" inputMode="decimal" required value={d.usage_qty_base} onChange={e=>changeComponent('usage_qty_base',e.target.value)}/></div>
        <div><label htmlFor="bom-material-unit">原料基本單位</label><input id="bom-material-unit" readOnly value={unit?unit.code+' · '+unit.name:'請先選擇原物料'}/></div>
        <div className="po-wide"><label htmlFor="bom-purpose">用途說明</label><textarea id="bom-purpose" value={d.purpose_note} onChange={e=>changeComponent('purpose_note',e.target.value)}/></div>
      </div><div className="form-actions" data-feedback-group>
        <ActionFeedback {...(lineFeedback.id===(editor.id||'new')?lineFeedback:{})} onReload={reload} busy={busy}/>
        <button className="button primary" type="submit" disabled={busy}>儲存明細</button>
        {r&&<button className="button danger" type="button" disabled={busy} onClick={()=>removeComponent(r)}>刪除此明細</button>}
        <button className="button" type="button" disabled={busy} onClick={()=>{setEditor(null);setLineFeedback({})}}>取消</button>
      </div>
    </form>
  }
  const productUnit=units.find(u=>u.id===products.find(p=>p.id===draft.product_id)?.base_unit_id)
  return <>
    <div className="page-heading"><div><div className="eyebrow">MANUFACTURING / PRODUCT BOM</div><h1>產品 BOM 用料表</h1><p>維護成品與原物料的標準用量；每次儲存一筆表頭或用料明細。</p></div><button className="button primary new-button" disabled={busy} onClick={newBom}>＋ 新增 BOM</button></div>
    <div className="page-meta"><span><span className="teal-dot"/>製造生產</span><span>草稿標準用料</span><span className="meta-right">損耗、替代料與發布規則待核定</span></div>
    <div className="po-layout">
      <section className="panel po-list" aria-label="BOM 清單"><div className="panel-title"><h2>BOM 清單 <span className="count">{rows.length}</span></h2></div>
        <div className="search-area" data-feedback-group><form className="search-bar" onSubmit={search}><label className="sr-only" htmlFor="bom-search">搜尋 BOM</label><input id="bom-search" value={query} onChange={e=>setQuery(e.target.value)} placeholder="搜尋成品、版號或用料"/><button type="submit">搜尋</button></form><ActionFeedback error={listError}/></div>
        {rows.map(r=><div className={'po-list-row '+(selected?.id===r.id?'current-row':'')} key={r.id} data-feedback-group><button className="record-link" disabled={busy} onClick={()=>open(r.id)} aria-label={'開啟 BOM '+r.product_code+' '+r.revision}><span>{r.product_code} · {r.revision}</span><strong>{r.product_name}</strong><span>{r.components.length} 筆用料 · {r.is_active?'啟用':'停用'} · {r.release_state}</span></button><ActionFeedback error={openError?.id===r.id?openError.message:''}/></div>)}
        {!rows.length&&<div className="empty-state"><h3>{applied?'沒有符合的 BOM':'尚無 BOM'}</h3><p>先建立成品與原料，再新增 BOM。</p></div>}
      </section>
      <div className="po-detail">
        <section className="panel po-header" aria-label="BOM 表頭"><div className="panel-title"><h2>{selected?selected.product_code+' · '+selected.revision:'新增 BOM'}</h2><span className="form-kind">{selected?.release_state||'DRAFT'} · 發布流程待核定</span></div>
          {headerEditing?<form onSubmit={saveHeader} aria-label="編輯 BOM 表頭"><div className="form-body po-fields">
            <div className="po-wide"><label htmlFor="bom-product">成品 *</label><select id="bom-product" required disabled={!!selected?.components.length} value={draft.product_id} onChange={e=>setDraft(d=>({...d,product_id:e.target.value}))}><option value="">請選擇成品</option>{products.map(p=><option key={p.id} value={p.id}>{p.code} · {p.name}</option>)}</select></div>
            <div><label htmlFor="bom-revision">BOM 版號 *</label><input id="bom-revision" required value={draft.revision} onChange={e=>setDraft(d=>({...d,revision:e.target.value}))}/></div>
            <div><label htmlFor="bom-basis">產出基準量 *</label><input id="bom-basis" type="text" inputMode="decimal" required value={draft.output_basis_qty} onChange={e=>setDraft(d=>({...d,output_basis_qty:e.target.value}))}/><p className="field-note">成品基本單位：{productUnit?productUnit.code+' · '+productUnit.name:'請先選擇成品'}；基準量須大於零。</p></div>
            <div><label htmlFor="bom-from">有效起日 *</label><input id="bom-from" type="date" required value={draft.valid_from} onChange={e=>setDraft(d=>({...d,valid_from:e.target.value}))}/></div>
            <div><label htmlFor="bom-to">有效迄日</label><input id="bom-to" type="date" value={draft.valid_to} onChange={e=>setDraft(d=>({...d,valid_to:e.target.value}))}/></div>
            <div><label htmlFor="bom-active">啟用狀態 *</label><select id="bom-active" required value={draft.is_active} onChange={e=>setDraft(d=>({...d,is_active:e.target.value as HeaderDraft['is_active']}))}><option value="">請選擇</option><option value="true">啟用</option><option value="false">停用</option></select></div>
          </div>{headerActions(true)}</form>:selected&&<><dl className="po-header-summary"><dt>成品</dt><dd>{selected.product_code} · {selected.product_name}</dd><dt>版號</dt><dd data-testid="bom-revision-value">{selected.revision}</dd><dt>產出基準</dt><dd data-testid="bom-basis-value">{basis}</dd><dt>有效期間</dt><dd>{selected.valid_from} ～ {selected.valid_to||'未指定迄日'}</dd><dt>啟用狀態</dt><dd>{selected.is_active?'啟用':'停用'}</dd></dl>{headerActions(false)}</>}
        </section>
        {selected&&<section className="panel po-lines" aria-label="BOM 用料明細"><div className="panel-title"><h2>用料明細 <span className="count">{selected.components.length}</span></h2></div>
          <p className="field-note po-policy">以下每筆用量均以產出 {basis} 為基準。可重複同一原料以區分用途。用量正負、損耗與替代料規則待核定；草稿用量不代表可領料數量。</p>
          {selected.components.map(r=><article className="po-line" key={r.id} data-testid={'bom-component-'+r.line_no}><div className="po-line-heading"><strong>用料 {r.line_no} · {r.material_code} · {r.material_name}</strong></div>
            <dl className="po-line-values"><dt>標準用量</dt><dd data-testid={'bom-usage-'+r.line_no}>{r.usage_qty_base} {r.material_base_unit_code} · {r.material_base_unit_name}</dd><dt>用途說明</dt><dd>{r.purpose_note||'—'}</dd></dl>
            {editor?.id===r.id?componentEditor():<div className="form-actions" data-feedback-group><ActionFeedback {...(lineFeedback.id===r.id?lineFeedback:{})} onReload={reload} busy={busy}/><button className="button" disabled={busy||headerEditing||!!editor||sealed} onClick={()=>editComponent(r)}>編輯用料 {r.line_no}</button><button className="button danger" disabled={busy||headerEditing||!!editor||sealed} onClick={()=>removeComponent(r)}>刪除用料 {r.line_no}</button></div>}
            <details className="audit"><summary>明細系統紀錄 · 版次 {r.row_version}</summary><dl><dt>UUID</dt><dd>{r.id}</dd><dt>建立時間</dt><dd>{r.created_at}</dd><dt>最後更新</dt><dd>{r.updated_at}</dd><dt>建立者 UUID</dt><dd>{r.created_by}</dd></dl></details>
          </article>)}
          {!selected.components.length&&<div className="po-no-lines">此 BOM 尚無用料明細。</div>}
          {editor&&!editor.id?componentEditor():<div className="form-actions" data-feedback-group><ActionFeedback {...(lineFeedback.id==='new'?lineFeedback:{})}/><button className="button primary" disabled={busy||headerEditing||!!editor||sealed} onClick={()=>editComponent()}>＋ 新增用料</button></div>}
          <details className="audit po-audit"><summary>系統紀錄 · BOM 版次 {selected.row_version}</summary><dl><dt>UUID</dt><dd data-testid="bom-uuid">{selected.id}</dd><dt>建立時間</dt><dd>{selected.created_at}</dd><dt>最後更新</dt><dd>{selected.updated_at}</dd><dt>建立者 UUID</dt><dd>{selected.created_by}</dd></dl></details>
        </section>}
      </div>
    </div>
    <footer className="page-footer">TIGER ERP <span>本機示範版 · 產品 BOM 用料表</span><span>發布、領料與 MRP 尚未實作</span></footer>
  </>
}
