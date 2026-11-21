import { useEffect, useState, type FormEvent } from 'react'
import { api } from './api'
import { ActionFeedback } from './ActionFeedback'

type Ref={id:string;code:string;name:string;is_active:boolean}
type Item=Ref&{base_unit_id:string}
type Audit={id:string;row_version:number;created_at:string;updated_at:string;created_by:string}
type HeaderDraft={product_id:string;revision:string;valid_from:string;valid_to:string;is_active:''|'true'|'false'}
type StepDraft={sequence_no:string;operation_id:string;work_center_id:string;setup_minutes:string;run_minutes_per_base:string}
type Step=Audit&{routing_id:string;sequence_no:string;operation_id:string;operation_code:string;operation_name:string;operation_instruction:string|null;operation_is_active:boolean;work_center_id:string;work_center_code:string;work_center_name:string;work_center_is_active:boolean;setup_minutes:string|null;run_minutes_per_base:string|null}
type Routing=Audit&{product_id:string;revision:string;valid_from:string;valid_to:string|null;is_active:boolean;release_state:string;product_code:string;product_name:string;product_base_unit_code:string;product_base_unit_name:string;steps:Step[]}
type BomRef={id:string;product_id:string;revision:string;release_state:string}
type Feedback={error?:string;notice?:string;id?:string}
type Editor={id:string|null;key:string;draft:StepDraft}
const blankHeader=():HeaderDraft=>({product_id:'',revision:'',valid_from:'',valid_to:'',is_active:''})
const blankStep=():StepDraft=>({sequence_no:'',operation_id:'',work_center_id:'',setup_minutes:'',run_minutes_per_base:''})
const headerDraft=(r:Routing):HeaderDraft=>({product_id:r.product_id,revision:r.revision,valid_from:r.valid_from,valid_to:r.valid_to||'',is_active:r.is_active?'true':'false'})
const stepDraft=(r:Step):StepDraft=>({sequence_no:r.sequence_no,operation_id:r.operation_id,work_center_id:r.work_center_id,setup_minutes:r.setup_minutes??'',run_minutes_per_base:r.run_minutes_per_base??''})

export function RoutingMaintenance({onConnection}:{onConnection:(online:boolean)=>void}){
  const [rows,setRows]=useState<Routing[]>([]),[selected,setSelected]=useState<Routing|null>(null)
  const [products,setProducts]=useState<Item[]>([]),[operations,setOperations]=useState<Ref[]>([]),[centers,setCenters]=useState<Ref[]>([]),[boms,setBoms]=useState<BomRef[]>([])
  const [draft,setDraft]=useState<HeaderDraft>(blankHeader),[headerEditing,setHeaderEditing]=useState(true)
  const [headerKey,setHeaderKey]=useState(()=>crypto.randomUUID()),[editor,setEditor]=useState<Editor|null>(null)
  const [busy,setBusy]=useState(false),[query,setQuery]=useState(''),[applied,setApplied]=useState('')
  const [headerFeedback,setHeaderFeedback]=useState<Feedback>({}),[stepFeedback,setStepFeedback]=useState<Feedback>({})
  const [listError,setListError]=useState(''),[openError,setOpenError]=useState<{id:string;message:string}|null>(null)
  async function loadList(q=applied){setRows(await api<Routing[]>('/routings?q='+encodeURIComponent(q)));setListError('');onConnection(true)}
  async function loadRefs(){const [p,o,w,b]=await Promise.all([api<Item[]>('/finished-goods'),api<Ref[]>('/operation-definitions'),api<Ref[]>('/work-centers'),api<BomRef[]>('/boms')]);setProducts(p);setOperations(o);setCenters(w);setBoms(b)}
  useEffect(()=>{Promise.all([loadList(''),loadRefs()]).then(()=>{const id=new URLSearchParams(window.location.hash.split('?')[1]||'').get('record');if(id)return open(id)}).catch(e=>{setListError(e.message);onConnection(false)})},[])
  function clearFeedback(){setHeaderFeedback({});setStepFeedback({});setOpenError(null)}
  function newRouting(){setSelected(null);setDraft(blankHeader());setHeaderKey(crypto.randomUUID());setHeaderEditing(true);setEditor(null);clearFeedback()}
  async function open(id:string){
    clearFeedback()
    try{const r=await api<Routing>('/routings/'+id);await loadRefs();setSelected(r);setDraft(headerDraft(r));setHeaderEditing(false);setEditor(null)}
    catch(e){setOpenError({id,message:(e as Error).message})}
  }
  async function refresh(id:string){const r=await api<Routing>('/routings/'+id);setSelected(r);setDraft(headerDraft(r));await loadList();return r}
  async function search(e:FormEvent){e.preventDefault();setApplied(query);try{await loadList(query)}catch(e){setListError((e as Error).message)}}
  function cancelHeader(){if(selected){setDraft(headerDraft(selected));setHeaderEditing(false);clearFeedback()}else newRouting()}
  async function saveHeader(e:FormEvent){
    e.preventDefault();setBusy(true);setHeaderFeedback({})
    const body={...draft,is_active:draft.is_active==='true',valid_to:draft.valid_to||null,...(selected?{row_version:selected.row_version}:{request_key:headerKey})}
    try{const r=await api<Routing>('/routings'+(selected?'/'+selected.id:''),{method:selected?'PUT':'POST',body:JSON.stringify(body)})
      setSelected(r);setDraft(headerDraft(r));setHeaderEditing(false);await loadList();setHeaderFeedback({notice:'途程表頭已儲存。'})
    }catch(e){setHeaderFeedback({error:(e as Error).message})}finally{setBusy(false)}
  }
  async function removeHeader(){
    if(!selected||!window.confirm('確定刪除此草稿途程？仍有步驟或引用的途程將受到保護。'))return
    setBusy(true);setHeaderFeedback({})
    try{await api('/routings/'+selected.id+'?row_version='+selected.row_version,{method:'DELETE'});newRouting();await loadList();setHeaderFeedback({notice:'途程已刪除。'})}
    catch(e){setHeaderFeedback({error:(e as Error).message})}finally{setBusy(false)}
  }
  function editStep(r?:Step){setEditor({id:r?.id||null,key:crypto.randomUUID(),draft:r?stepDraft(r):blankStep()});clearFeedback()}
  function changeStep(k:keyof StepDraft,v:string){setEditor(e=>e?{...e,draft:{...e.draft,[k]:v}}:null)}
  async function saveStep(e:FormEvent){
    e.preventDefault();if(!selected||!editor)return
    setBusy(true);setStepFeedback({})
    const r=editor.id?selected.steps.find(s=>s.id===editor.id):null
    const body={...editor.draft,setup_minutes:editor.draft.setup_minutes||null,run_minutes_per_base:editor.draft.run_minutes_per_base||null,routing_version:selected.row_version,...(r?{row_version:r.row_version}:{request_key:editor.key})}
    try{const s=await api<Step>('/routings/'+selected.id+'/steps'+(r?'/'+r.id:''),{method:r?'PUT':'POST',body:JSON.stringify(body)})
      await refresh(selected.id);setEditor(null);setStepFeedback({id:s.id,notice:'途程步驟已儲存。'})
    }catch(e){setStepFeedback({id:editor.id||'new',error:(e as Error).message})}finally{setBusy(false)}
  }
  async function removeStep(r:Step){
    if(!selected||!window.confirm('確定刪除此筆未被引用的草稿步驟？'))return
    setBusy(true);setStepFeedback({})
    try{await api('/routings/'+selected.id+'/steps/'+r.id+'?row_version='+r.row_version+'&routing_version='+selected.row_version,{method:'DELETE'})
      await refresh(selected.id);setEditor(null);setStepFeedback({id:'new',notice:'途程步驟已刪除。'})
    }catch(e){setStepFeedback({id:r.id,error:(e as Error).message})}finally{setBusy(false)}
  }
  const reload=selected?()=>{void open(selected.id)}:undefined
  const productUnit=selected?selected.product_base_unit_code+' · '+selected.product_base_unit_name:''
  const headerActions=(editing:boolean)=><div className="form-actions" data-feedback-group>
    <ActionFeedback {...headerFeedback} onReload={reload} busy={busy}/>
    {editing?<button className="button primary" type="submit" disabled={busy}>儲存表頭</button>:<button className="button primary" disabled={busy||!!editor} onClick={()=>{setDraft(headerDraft(selected!));setHeaderEditing(true);clearFeedback()}}>編輯表頭</button>}
    {selected&&<button className="button danger" type="button" disabled={busy||!!editor} onClick={removeHeader}>刪除途程</button>}
    {editing&&<button className="button" type="button" disabled={busy} onClick={cancelHeader}>取消</button>}
  </div>
  const stepEditor=()=>{
    if(!editor||!selected)return null
    const r=editor.id?selected.steps.find(s=>s.id===editor.id):null,d=editor.draft
    return <form className="po-line-editor" onSubmit={saveStep} aria-label={r?'編輯途程步驟':'新增途程步驟'}>
      <div className="form-body po-fields">
        <div><label htmlFor="routing-sequence">步序 *</label><input id="routing-sequence" type="text" inputMode="numeric" pattern="-?[0-9]+" required value={d.sequence_no} onChange={e=>changeStep('sequence_no',e.target.value)}/></div>
        <div><label htmlFor="routing-operation">製程作業 *</label><select id="routing-operation" required value={d.operation_id} onChange={e=>changeStep('operation_id',e.target.value)}><option value="">請選擇作業</option>{operations.filter(o=>o.is_active||o.id===r?.operation_id).map(o=><option key={o.id} value={o.id}>{o.code} · {o.name}{o.is_active?'':'（停用，保留既有引用）'}</option>)}</select></div>
        <div className="po-wide"><label htmlFor="routing-center">工作中心 *</label><select id="routing-center" required value={d.work_center_id} onChange={e=>changeStep('work_center_id',e.target.value)}><option value="">請選擇工作中心</option>{centers.filter(w=>w.is_active||w.id===r?.work_center_id).map(w=><option key={w.id} value={w.id}>{w.code} · {w.name}{w.is_active?'':'（停用，保留既有引用）'}</option>)}</select></div>
        <div><label htmlFor="routing-setup">整備時間（分鐘／批）</label><input id="routing-setup" type="text" inputMode="decimal" value={d.setup_minutes} onChange={e=>changeStep('setup_minutes',e.target.value)} placeholder="未估可留白"/></div>
        <div><label htmlFor="routing-run">單位工時（分鐘／成品基本單位）</label><input id="routing-run" type="text" inputMode="decimal" value={d.run_minutes_per_base} onChange={e=>changeStep('run_minutes_per_base',e.target.value)} placeholder="未估可留白"/><p className="field-note">成品基本單位：{productUnit}</p></div>
        <p className="field-note po-wide">步序為可修改整數，不自動重排；交換順序請先使用空閒步序。工時留白表示未估，0 為明確輸入；正負值的執行規則待核定。</p>
      </div><div className="form-actions" data-feedback-group>
        <ActionFeedback {...(stepFeedback.id===(editor.id||'new')?stepFeedback:{})} onReload={reload} busy={busy}/>
        <button className="button primary" type="submit" disabled={busy}>儲存步驟</button>
        {r&&<button className="button danger" type="button" disabled={busy} onClick={()=>removeStep(r)}>刪除此步驟</button>}
        <button className="button" type="button" disabled={busy} onClick={()=>{setEditor(null);setStepFeedback({})}}>取消</button>
      </div>
    </form>
  }
  return <>
    <div className="page-heading"><div><div className="eyebrow">MANUFACTURING / ROUTINGS</div><h1>製程設定</h1><p>依成品與版號維護有序步驟，選取可重用作業及工作中心。</p></div><button className="button primary new-button" disabled={busy} onClick={newRouting}>＋ 新增途程</button></div>
    <div className="page-meta"><span><span className="teal-dot"/>製造生產</span><span>草稿途程維護</span><a className="meta-right" href="#operation-definitions">製程作業定義維護 ↗</a></div>
    <div className="po-layout">
      <section className="panel po-list" aria-label="途程清單"><div className="panel-title"><h2>途程清單 <span className="count">{rows.length}</span></h2></div>
        <div className="search-area" data-feedback-group><form className="search-bar" onSubmit={search}><label className="sr-only" htmlFor="routing-search">搜尋途程</label><input id="routing-search" value={query} onChange={e=>setQuery(e.target.value)} placeholder="搜尋成品、版號、作業或中心"/><button type="submit">搜尋</button></form><ActionFeedback error={listError}/></div>
        {rows.map(r=><div className={'po-list-row '+(selected?.id===r.id?'current-row':'')} key={r.id} data-feedback-group><button className="record-link" disabled={busy} onClick={()=>open(r.id)} aria-label={'開啟途程 '+r.product_code+' '+r.revision}><span>{r.product_code} · {r.revision}</span><strong>{r.product_name}</strong><span>{r.steps.length} 筆步驟 · {r.is_active?'啟用':'停用'} · {r.release_state}</span></button><ActionFeedback error={openError?.id===r.id?openError.message:''}/></div>)}
        {!rows.length&&<div className="empty-state"><h3>{applied?'沒有符合的途程':'尚無途程'}</h3><p>先建立成品、作業定義及工作中心，再維護途程步驟。</p></div>}
      </section>
      <div className="po-detail">
        <section className="panel po-header" aria-label="途程表頭"><div className="panel-title"><h2>{selected?selected.product_code+' · '+selected.revision:'新增途程'}</h2><span className="form-kind">DRAFT · 發布流程待核定</span></div>
          {headerEditing?<form onSubmit={saveHeader} aria-label="編輯途程表頭"><div className="form-body po-fields">
            <div className="po-wide"><label htmlFor="routing-product">成品 *</label><select id="routing-product" required disabled={!!selected?.steps.length} value={draft.product_id} onChange={e=>setDraft(d=>({...d,product_id:e.target.value}))}><option value="">請選擇成品</option>{products.map(p=><option key={p.id} value={p.id}>{p.code} · {p.name}</option>)}</select></div>
            <div><label htmlFor="routing-revision">途程版號 *</label><input id="routing-revision" required value={draft.revision} onChange={e=>setDraft(d=>({...d,revision:e.target.value}))}/></div>
            <div><label htmlFor="routing-active">啟用狀態 *</label><select id="routing-active" required value={draft.is_active} onChange={e=>setDraft(d=>({...d,is_active:e.target.value as HeaderDraft['is_active']}))}><option value="">請選擇</option><option value="true">啟用</option><option value="false">停用</option></select></div>
            <div><label htmlFor="routing-from">有效起日 *</label><input id="routing-from" type="date" required value={draft.valid_from} onChange={e=>setDraft(d=>({...d,valid_from:e.target.value}))}/></div>
            <div><label htmlFor="routing-to">有效迄日</label><input id="routing-to" type="date" value={draft.valid_to} onChange={e=>setDraft(d=>({...d,valid_to:e.target.value}))}/></div>
          </div>{headerActions(true)}</form>:selected&&<><dl className="po-header-summary"><dt>成品</dt><dd>{selected.product_code} · {selected.product_name}</dd><dt>版號</dt><dd data-testid="routing-revision-value">{selected.revision}</dd><dt>單位工時基準</dt><dd data-testid="routing-time-basis">每 1 {productUnit} 成品所需分鐘</dd><dt>有效期間</dt><dd>{selected.valid_from} ～ {selected.valid_to||'未指定迄日'}</dd><dt>啟用狀態</dt><dd>{selected.is_active?'啟用':'停用'}</dd></dl>{headerActions(false)}</>}
        </section>
        {selected&&<section className="panel po-lines" aria-label="途程步驟"><div className="panel-title"><h2>途程步驟 <span className="count">{selected.steps.length}</span></h2></div>
          <p className="field-note po-policy">所有步驟依步序排列。相同作業或工作中心可重複引用；每次只儲存一筆步驟。途程與 BOM 分別維護，不自動選版、排程或領料。</p>
          {selected.steps.map(r=><article className="po-line" key={r.id} data-testid={'routing-step-'+r.id} data-sequence={r.sequence_no}><div className="po-line-heading"><strong>步序 {r.sequence_no} · {r.operation_code} · {r.operation_name}</strong></div>
            <dl className="po-line-values"><dt>工作中心</dt><dd>{r.work_center_code} · {r.work_center_name}{r.work_center_is_active?'':'（停用）'}</dd><dt>作業說明</dt><dd>{r.operation_instruction||'—'}{r.operation_is_active?'':'（作業已停用）'}</dd><dt>每批整備分鐘</dt><dd data-testid="routing-setup-value">{r.setup_minutes??'未估'}</dd><dt>單位工時</dt><dd data-testid="routing-run-value">{r.run_minutes_per_base===null?'未估':r.run_minutes_per_base+' 分鐘／'+productUnit}</dd></dl>
            {editor?.id===r.id?stepEditor():<div className="form-actions" data-feedback-group><ActionFeedback {...(stepFeedback.id===r.id?stepFeedback:{})} onReload={reload} busy={busy}/><button className="button" disabled={busy||headerEditing||!!editor} onClick={()=>editStep(r)}>編輯步驟 {r.sequence_no}</button><button className="button danger" disabled={busy||headerEditing||!!editor} onClick={()=>removeStep(r)}>刪除步驟 {r.sequence_no}</button></div>}
            <details className="audit"><summary>步驟系統紀錄 · 版次 {r.row_version}</summary><dl><dt>UUID</dt><dd>{r.id}</dd><dt>建立時間</dt><dd>{r.created_at}</dd><dt>最後更新</dt><dd>{r.updated_at}</dd><dt>建立者 UUID</dt><dd>{r.created_by}</dd></dl></details>
          </article>)}
          {!selected.steps.length&&<div className="po-no-lines">此途程尚無步驟。</div>}
          {editor&&!editor.id?stepEditor():<div className="form-actions" data-feedback-group><ActionFeedback {...(stepFeedback.id==='new'?stepFeedback:{})}/><button className="button primary" disabled={busy||headerEditing||!!editor} onClick={()=>editStep()}>＋ 新增步驟</button></div>}
          <details className="audit po-audit"><summary>系統紀錄 · 途程版次 {selected.row_version}</summary><dl><dt>UUID</dt><dd data-testid="routing-uuid">{selected.id}</dd><dt>建立時間</dt><dd>{selected.created_at}</dd><dt>最後更新</dt><dd>{selected.updated_at}</dd><dt>建立者 UUID</dt><dd>{selected.created_by}</dd></dl></details>
        </section>}
        {selected&&<section className="panel" aria-label="同成品 BOM 參考"><div className="panel-title"><h2>同成品 BOM 參考</h2></div><div className="form-body"><p className="field-note">僅供查看，沒有綁定或變更 BOM；工單選用與版本承諾規則待核定。</p>{boms.filter(b=>b.product_id===selected.product_id).map(b=><p key={b.id}><a href={'#boms?record='+b.id}>{b.revision} · {b.release_state} ↗</a></p>)}{!boms.some(b=>b.product_id===selected.product_id)&&<p className="muted">此成品尚無 BOM；仍可獨立維護途程。</p>}</div></section>}
      </div>
    </div>
    <footer className="page-footer">TIGER ERP <span>本機示範版 · 製程設定</span><span>發布、排程與製造執行尚未實作</span></footer>
  </>
}
