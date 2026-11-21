import { useEffect, useState, type FormEvent } from 'react'
import { api } from './api'
import { ActionFeedback } from './ActionFeedback'

type Ref={id:string;code:string;name:string}
type Offer={id:string;supplier_id:string;item_id:string;unit_price:string;is_active:boolean;
  supplier_code:string;supplier_name:string;item_code:string;item_name:string;
  row_version:number;created_at:string;created_by:string;updated_at:string}
type Draft=Pick<Offer,'supplier_id'|'item_id'|'unit_price'|'is_active'>
const blank=():Draft=>({supplier_id:'',item_id:'',unit_price:'',is_active:true})
const fromRecord=(row:Offer):Draft=>({supplier_id:row.supplier_id,item_id:row.item_id,unit_price:row.unit_price,is_active:row.is_active})
const time=(value:string)=>new Date(value).toLocaleString('zh-TW',{hour12:false})

export function OfferMaintenance({onConnection}:{onConnection:(online:boolean)=>void}){
  const [records,setRecords]=useState<Offer[]>([]),[suppliers,setSuppliers]=useState<Ref[]>([]),[materials,setMaterials]=useState<Ref[]>([])
  const [draft,setDraft]=useState<Draft>(blank),[editing,setEditing]=useState<Offer|null>(null)
  const [query,setQuery]=useState(''),[applied,setApplied]=useState(''),[busy,setBusy]=useState(false)
  const [error,setError]=useState(''),[notice,setNotice]=useState('')
  const [listError,setListError]=useState(''),[openError,setOpenError]=useState<{id:string;message:string}|null>(null)
  async function load(q=applied){
    const [rows,ss,ms]=await Promise.all([api<Offer[]>('/supplier-offers?q='+encodeURIComponent(q)),api<Ref[]>('/suppliers'),api<Ref[]>('/raw-materials')])
    setRecords(rows);setSuppliers(ss);setMaterials(ms);onConnection(true);setListError('')
  }
  useEffect(()=>{load('').catch(e=>{setListError(e.message);onConnection(false)})},[])
  function cancel(){setEditing(null);setDraft(blank());setError('');setNotice('')}
  async function open(id:string){
    setError('');setNotice('');setOpenError(null)
    try{const row=await api<Offer>('/supplier-offers/'+id);setEditing(row);setDraft(fromRecord(row))}
    catch(e){setOpenError({id,message:(e as Error).message})}
  }
  async function search(event:FormEvent){
    event.preventDefault();setApplied(query)
    try{await load(query);setListError('')}catch(e){setListError((e as Error).message);onConnection(false)}
  }
  async function save(event:FormEvent){
    event.preventDefault();setBusy(true);setError('');setNotice('')
    try{
      const row=await api<Offer>('/supplier-offers'+(editing?'/'+editing.id:''),{method:editing?'PUT':'POST',
        body:JSON.stringify({...draft,...(editing?{row_version:editing.row_version}:{})})})
      setEditing(row);setDraft(fromRecord(row));await load();setNotice(editing?'變更已儲存。':'供料設定已新增。')
    }catch(e){setError((e as Error).message)}finally{setBusy(false)}
  }
  async function remove(){
    if(!editing||!window.confirm('確定刪除此供料設定？供應商與原料資料會保留。'))return
    setBusy(true);setError('');setNotice('')
    try{await api('/supplier-offers/'+editing.id+'?row_version='+editing.row_version,{method:'DELETE'});cancel();await load();setNotice('供料設定已刪除。')}
    catch(e){setError((e as Error).message)}finally{setBusy(false)}
  }
  return <>
    <div className="page-heading"><div><div className="eyebrow">MASTER DATA / SUPPLIER OFFERINGS</div><h1>供應商供料設定</h1>
      <p>連結既有供應商與原物料，維護每筆供料單價。</p></div><button className="button primary new-button" disabled={busy} onClick={cancel} aria-label="新增供料設定"><span>＋</span>新增設定</button></div>
    <div className="page-meta"><span><span className="teal-dot"/>基本資料</span><span>供應商 × 原物料</span><span className="meta-right">採購流程尚未實作</span></div>
    <div className="content-grid">
      <section className="panel list-panel" aria-label="供料設定清單">
        <div className="panel-title"><h2>供料設定清單 <span className="count">{records.length}</span></h2><span className="muted">目前顯示</span></div>
        <div className="search-area" data-feedback-group><form className="search-bar" onSubmit={search}><label className="sr-only" htmlFor="offer-search">搜尋供應商或原料</label><span aria-hidden="true">⌕</span>
          <input id="offer-search" value={query} onChange={e=>setQuery(e.target.value)} placeholder="搜尋供應商或原料代碼、名稱"/><button type="submit">搜尋</button></form><ActionFeedback error={listError}/></div>
        <div className="table-wrap offer-table"><table><thead><tr><th>供應商 / 原料</th><th>供料單價</th><th>狀態</th></tr></thead><tbody>
          {records.map(row=><tr key={row.id} className={editing?.id===row.id?'current-row':''}><td data-feedback-group><button className="record-link" disabled={busy} onClick={()=>open(row.id)} aria-label={'開啟 '+row.supplier_code+' / '+row.item_code}>
            <span>{row.supplier_code} · {row.supplier_name}</span><strong>{row.item_code} · {row.item_name}</strong></button><ActionFeedback error={openError?.id===row.id?openError.message:''}/></td>
            <td className="exact-price">{row.unit_price}</td><td><span className={'status '+(row.is_active?'active':'inactive')}>{row.is_active?'啟用':'停用'}</span></td></tr>)}
        </tbody></table></div>
        {!records.length&&<div className="empty-state"><span className="empty-icon">▤</span><h3>{applied?'沒有符合的供料設定':'建立第一筆供料設定'}</h3><p>先於主檔維護供應商與原物料，再選取建立關聯。</p></div>}
        <div className="table-footer">移除設定不會刪除供應商或原料；資料會保存在此電腦。</div>
      </section>
      <section className="panel editor-panel" aria-label="供料設定資料">
        <div className="panel-title"><h2>{editing?'編輯供料設定':'新增供料設定'}</h2><span className="form-kind">{editing?'編輯中':'新資料'}</span></div>
        <form onSubmit={save}><div className="form-body">
          
          
          <label htmlFor="offer-supplier">供應商 <span className="required">*</span></label>
          <select id="offer-supplier" required value={draft.supplier_id} onChange={e=>setDraft(d=>({...d,supplier_id:e.target.value}))}>
            <option value="">請選擇供應商</option>{suppliers.map(row=><option key={row.id} value={row.id}>{row.code} · {row.name}</option>)}</select>
          <label htmlFor="offer-material">原物料 <span className="required">*</span></label>
          <select id="offer-material" required value={draft.item_id} onChange={e=>setDraft(d=>({...d,item_id:e.target.value}))}>
            <option value="">請選擇原物料</option>{materials.map(row=><option key={row.id} value={row.id}>{row.code} · {row.name}</option>)}</select>
          <label htmlFor="offer-price">供料單價 <span className="required">*</span></label>
          <input id="offer-price" type="text" inputMode="decimal" required value={draft.unit_price} onChange={e=>setDraft(d=>({...d,unit_price:e.target.value}))}/>
          <p className="field-note">請輸入十進位數字，可保留零、負數與小數。僅保存供料單價；採購、換算與計價規則尚未實作。</p>
          <label className="master-active"><input type="checkbox" checked={draft.is_active} onChange={e=>setDraft(d=>({...d,is_active:e.target.checked}))}/> 啟用此供料設定</label>
          {editing&&<details className="audit"><summary>系統紀錄 · 版次 {editing.row_version}</summary><dl>
            <dt>UUID</dt><dd data-testid="offer-uuid">{editing.id}</dd><dt>建立時間</dt><dd>{time(editing.created_at)}</dd>
            <dt>最後更新</dt><dd>{time(editing.updated_at)}</dd><dt>建立者</dt><dd>本機示範操作員（未驗證）</dd></dl></details>}
        </div><div className="form-actions" data-feedback-group>
          <ActionFeedback error={error} notice={notice} onReload={editing?()=>open(editing.id):undefined} busy={busy}/>
          <button className="button primary" disabled={busy} type="submit">{busy?'儲存中…':editing?'儲存變更':'儲存供料設定'}</button>
          {editing&&<button className="button danger" disabled={busy} type="button" onClick={remove}>刪除供料設定</button>}
          <button className="button" disabled={busy} type="button" onClick={cancel}>取消</button>
        </div></form>
      </section>
    </div>
    <footer className="page-footer">TIGER ERP <span>本機示範版 · 供應商供料設定</span><span>其他作業尚未實作</span></footer>
  </>
}
