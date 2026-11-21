import { useEffect, useState, type FormEvent } from 'react'
import { api } from './api'
import { ActionFeedback } from './ActionFeedback'

type Field = {key:string;label:string;type?:'textarea'|'email'|'currency'|'boolean'}
const partyFields:Field[]=[
  {key:'tax_identifier',label:'稅籍號碼'},{key:'contact_name',label:'聯絡人'},
  {key:'phone',label:'電話'},{key:'email',label:'電子郵件',type:'email'},
  {key:'payment_terms',label:'付款條件'},{key:'currency_id',label:'預設幣別',type:'currency'},
]
export const masterDefinitions={
  customers:{noun:'客戶',title:'客戶資料維護',eyebrow:'CUSTOMERS',fields:[...partyFields,
    {key:'billing_address',label:'帳單地址',type:'textarea'},
    {key:'default_ship_address',label:'預設送貨地址',type:'textarea'}] as Field[]},
  suppliers:{noun:'供應商',title:'供應商資料維護',eyebrow:'SUPPLIERS',fields:[...partyFields,
    {key:'address',label:'地址',type:'textarea'}] as Field[]},
  warehouses:{noun:'倉庫',title:'倉庫資料維護',eyebrow:'WAREHOUSES',fields:[
    {key:'address',label:'地址',type:'textarea'},{key:'mrp_included',label:'納入 MRP',type:'boolean'}] as Field[]},
  'operation-definitions':{noun:'製程作業',title:'製程作業定義',eyebrow:'OPERATION DEFINITIONS',fields:[
    {key:'instruction',label:'作業說明',type:'textarea'}] as Field[]},
  'work-centers':{noun:'工作中心',title:'工作中心維護',eyebrow:'WORK CENTERS',fields:[
    {key:'description',label:'中心說明',type:'textarea'}] as Field[]},
}
export type MasterKind=keyof typeof masterDefinitions
type MasterRecord={id:string;code:string;name:string;row_version:number;is_active:boolean;
  created_at:string;updated_at:string;created_by:string;[key:string]:string|boolean|number|null}
type Draft=Record<string,string|boolean>
type Currency={id:string;code:string;name:string;row_version:number}
const time=(value:string)=>new Date(value).toLocaleString('zh-TW',{hour12:false})

export function MasterMaintenance({resource,onConnection}:{resource:MasterKind;onConnection:(online:boolean)=>void}){
  const definition=masterDefinitions[resource], noun=definition.noun, endpoint='/'+resource
  const needsCurrency=definition.fields.some(f=>f.type==='currency')
  const blank=():Draft=>Object.fromEntries([['code',''],['name',''],['is_active',true],...definition.fields.map(f=>[f.key,''])])
  const [records,setRecords]=useState<MasterRecord[]>([]),[currencies,setCurrencies]=useState<Currency[]>([])
  const [draft,setDraft]=useState<Draft>(blank),[editing,setEditing]=useState<MasterRecord|null>(null)
  const [query,setQuery]=useState(''),[applied,setApplied]=useState(''),[busy,setBusy]=useState(false)
  const [error,setError]=useState(''),[notice,setNotice]=useState('')
  const [listError,setListError]=useState(''),[openError,setOpenError]=useState<{id:string;message:string}|null>(null)
  const [currencyDialog,setCurrencyDialog]=useState(false),[currencyCode,setCurrencyCode]=useState(''),[currencyName,setCurrencyName]=useState('')
  const [currencyBusy,setCurrencyBusy]=useState(false),[currencyError,setCurrencyError]=useState('')
  const fromRecord=(record:MasterRecord):Draft=>Object.fromEntries([['code',record.code],['name',record.name],['is_active',record.is_active],
    ...definition.fields.map(f=>[f.key,f.type==='boolean'?(record[f.key]?'true':'false'):record[f.key]??''])])
  async function load(q=applied){
    const [rows,refs]=await Promise.all([api<MasterRecord[]>(endpoint+'?q='+encodeURIComponent(q)),
      needsCurrency?api<Currency[]>('/currencies'):Promise.resolve([] as Currency[])])
    setRecords(rows);setCurrencies(refs);onConnection(true);setListError('')
  }
  useEffect(()=>{load('').catch(e=>{setListError(e.message);onConnection(false)})},[resource])
  function change(key:string,value:string|boolean){setDraft(previous=>({...previous,[key]:value}))}
  function cancel(){setEditing(null);setDraft(blank());setError('');setNotice('')}
  async function open(id:string){
    setError('');setNotice('');setOpenError(null)
    try{const row=await api<MasterRecord>(endpoint+'/'+id);setEditing(row);setDraft(fromRecord(row))}
    catch(e){setOpenError({id,message:(e as Error).message})}
  }
  async function search(event:FormEvent){
    event.preventDefault();setApplied(query)
    try{await load(query);setListError('')}catch(e){setListError((e as Error).message);onConnection(false)}
  }
  async function save(event:FormEvent){
    event.preventDefault();setBusy(true);setError('');setNotice('')
    const body:Record<string,string|boolean|number|null>={code:draft.code,name:draft.name,is_active:draft.is_active}
    for(const field of definition.fields)body[field.key]=field.type==='boolean'?draft[field.key]==='true':draft[field.key]||null
    if(editing)body.row_version=editing.row_version
    try{
      const row=await api<MasterRecord>(endpoint+(editing?'/'+editing.id:''),{method:editing?'PUT':'POST',body:JSON.stringify(body)})
      setEditing(row);setDraft(fromRecord(row));await load();setNotice(editing?'變更已儲存。':noun+'已新增。')
    }catch(e){setError((e as Error).message)}finally{setBusy(false)}
  }
  async function remove(){
    if(!editing||!window.confirm('確定刪除'+noun+'「'+editing.code+'」？已被引用的資料將受到保護。'))return
    setBusy(true);setError('');setNotice('')
    try{await api(endpoint+'/'+editing.id+'?row_version='+editing.row_version,{method:'DELETE'});cancel();await load();setNotice(noun+'已刪除。')}
    catch(e){setError((e as Error).message)}finally{setBusy(false)}
  }
  function showCurrency(){setCurrencyCode('');setCurrencyName('');setCurrencyError('');setCurrencyDialog(true)}
  async function addCurrency(event:FormEvent){
    event.preventDefault();setCurrencyBusy(true);setCurrencyError('')
    try{const row=await api<Currency>('/currencies',{method:'POST',body:JSON.stringify({code:currencyCode,name:currencyName})})
      change('currency_id',row.id);await load();setCurrencyDialog(false);setNotice('幣別已新增並選取。')
    }catch(e){setCurrencyError((e as Error).message)}finally{setCurrencyBusy(false)}
  }
  return <>
    <div className="page-heading"><div><div className="eyebrow">{resource==='operation-definitions'?'MANUFACTURING':'MASTER DATA'} / {definition.eyebrow}</div><h1>{definition.title}</h1>
      <p>維護{noun}基本資料，保留穩定識別與系統紀錄。</p></div>
      <button className="button primary new-button" disabled={busy} onClick={cancel} aria-label={'新增'+noun}><span>＋</span>新增{noun}</button></div>
    <div className="page-meta"><span><span className="teal-dot"/>{resource==='operation-definitions'?'製造生產':'基本資料'}</span><span>僅維護{noun}主檔</span><span className="meta-right">{resource==='operation-definitions'?<a href="#routings">返回製程設定 ↗</a>:'交易流程尚未實作'}</span></div>
    <div className="content-grid">
      <section className="panel list-panel" aria-label={noun+'清單'}>
        <div className="panel-title"><h2>{noun}清單 <span className="count">{records.length}</span></h2><span className="muted">目前顯示</span></div>
        <div className="search-area" data-feedback-group><form className="search-bar" onSubmit={search}><label className="sr-only" htmlFor="master-search">搜尋{noun}</label><span aria-hidden="true">⌕</span>
          <input id="master-search" value={query} onChange={e=>setQuery(e.target.value)} placeholder={'搜尋代碼、名稱'+(needsCurrency?'或聯絡資料':'')}/><button type="submit">搜尋</button></form><ActionFeedback error={listError}/></div>
        <div className="table-wrap"><table><thead><tr><th>代碼 / 名稱</th><th>狀態</th><th><span className="sr-only">操作</span></th></tr></thead><tbody>
          {records.map(row=><tr key={row.id} className={editing?.id===row.id?'current-row':''}><td data-feedback-group>
            <button className="record-link" disabled={busy} onClick={()=>open(row.id)} aria-label={'開啟 '+row.code}><span>{row.code}</span><strong>{row.name}</strong></button><ActionFeedback error={openError?.id===row.id?openError.message:''}/></td>
            <td><span className={'status '+(row.is_active?'active':'inactive')}>{row.is_active?'啟用':'停用'}</span></td>
            <td><button className="row-open" disabled={busy} onClick={()=>open(row.id)} aria-label={'編輯 '+row.code}>↗</button></td></tr>)}
        </tbody></table></div>
        {!records.length&&<div className="empty-state"><span className="empty-icon">▤</span><h3>{applied?'沒有符合的'+noun:'建立你的第一筆'+noun}</h3><p>使用右側表單維護資料。</p></div>}
        <div className="table-footer">資料會保存在此電腦，重新整理後仍可讀取。</div>
      </section>
      <section className="panel editor-panel" aria-label={noun+'資料'}>
        <div className="panel-title"><h2>{(editing?'編輯':'新增')+noun}</h2><span className="form-kind">{editing?'編輯中':'新資料'}</span></div>
        <form onSubmit={save}><div className="form-body">
          
          
          <label htmlFor="master-code">代碼 <span className="required">*</span></label><input id="master-code" required value={String(draft.code)} onChange={e=>change('code',e.target.value)}/>
          <label htmlFor="master-name">名稱 <span className="required">*</span></label><input id="master-name" required value={String(draft.name)} onChange={e=>change('name',e.target.value)}/>
          {definition.fields.map(field=><div className="master-field" key={field.key}>
            <div className="field-label"><label htmlFor={'master-'+field.key}>{field.label}{(field.type==='currency'||field.type==='boolean')&&<span className="required"> *</span>}</label>
              {field.type==='currency'&&<button type="button" disabled={busy} onClick={showCurrency}>＋ 新增幣別</button>}</div>
            {field.type==='textarea'?<textarea id={'master-'+field.key} rows={3} value={String(draft[field.key])} onChange={e=>change(field.key,e.target.value)}/>:
             field.type==='currency'?<select id={'master-'+field.key} required value={String(draft[field.key])} onChange={e=>change(field.key,e.target.value)}>
               <option value="">請選擇幣別</option>{currencies.map(c=><option key={c.id} value={c.id}>{c.code} · {c.name}</option>)}</select>:
             field.type==='boolean'?<select id={'master-'+field.key} required value={String(draft[field.key])} onChange={e=>change(field.key,e.target.value)}>
               <option value="">請選擇</option><option value="true">是</option><option value="false">否</option></select>:
             <input id={'master-'+field.key} type={field.type==='email'?'email':'text'} value={String(draft[field.key])} onChange={e=>change(field.key,e.target.value)}/>}
          </div>)}
          {resource==='warehouses'&&<p className="field-note">納入 MRP 僅保存設定；MRP 與庫存異動尚未實作。</p>}
          {needsCurrency&&<p className="field-note">預設幣別僅用於選取；金額精度、捨入與稅務規則尚待核定。</p>}
          <label className="master-active"><input type="checkbox" checked={Boolean(draft.is_active)} onChange={e=>change('is_active',e.target.checked)}/> 啟用此{noun}</label>
          {editing&&<details className="audit"><summary>系統紀錄 · 版次 {editing.row_version}</summary><dl>
            <dt>UUID</dt><dd data-testid="master-uuid">{editing.id}</dd><dt>建立時間</dt><dd>{time(editing.created_at)}</dd>
            <dt>最後更新</dt><dd>{time(editing.updated_at)}</dd><dt>建立者</dt><dd>本機示範操作員（未驗證）</dd></dl></details>}
        </div><div className="form-actions" data-feedback-group>
          <ActionFeedback error={error} notice={notice} onReload={editing?()=>open(editing.id):undefined} busy={busy}/>
          <button className="button primary" disabled={busy} type="submit">{busy?'儲存中…':editing?'儲存變更':'儲存'+noun}</button>
          {!editing&&resource==='operation-definitions'&&<button className="button" disabled={busy} type="button" onClick={cancel}>取消</button>}
          {editing&&<><button className="button danger" disabled={busy} type="button" onClick={remove}>刪除{noun}</button>
            <button className="button" disabled={busy} type="button" onClick={cancel}>取消</button></>}
        </div></form>
      </section>
    </div>
    <footer className="page-footer">TIGER ERP <span>本機示範版 · {definition.title}</span><span>其他作業尚未實作</span></footer>
    {currencyDialog&&<div className="modal-backdrop"><section className="modal" role="dialog" aria-modal="true" aria-labelledby="currency-title">
      <div className="panel-title"><h2 id="currency-title">幣別小工具</h2><button className="close-button" disabled={currencyBusy} aria-label="關閉幣別小工具" onClick={()=>setCurrencyDialog(false)}>×</button></div>
      <div className="modal-body"><p className="muted">只建立幣別代碼與名稱，不設定尚待核定的金額規則。</p>
        
        <form onSubmit={addCurrency}><label htmlFor="currency-code">幣別代碼</label><input id="currency-code" autoFocus required value={currencyCode} onChange={e=>setCurrencyCode(e.target.value)}/>
          <label htmlFor="currency-name">幣別名稱</label><input id="currency-name" required value={currencyName} onChange={e=>setCurrencyName(e.target.value)}/>
          <div className="form-actions" data-feedback-group><ActionFeedback error={currencyError}/><button className="button primary" type="submit" disabled={currencyBusy}>新增並選取</button>
            <button className="button" type="button" disabled={currencyBusy} onClick={()=>setCurrencyDialog(false)}>取消</button></div></form>
      </div></section></div>}
  </>
}
