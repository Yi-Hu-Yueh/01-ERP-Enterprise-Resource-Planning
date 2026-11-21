import {DocumentApproval} from './DocumentApproval'
import { useEffect, useState, type FormEvent } from 'react'
import { api } from './api'
import { ActionFeedback } from './ActionFeedback'

type Ref={id:string;code:string;name:string}
type Supplier=Ref&{currency_id:string;payment_terms:string|null}
type Material=Ref&{base_unit_id:string;specification:string|null}
type Offer={id:string;supplier_id:string;item_id:string;unit_price:string;is_active:boolean}
type Audit={id:string;row_version:number;created_at:string;updated_at:string;created_by:string}
type HeaderDraft={document_no:string;supplier_id:string;supplier_name_snapshot:string;currency_id:string;order_date:string;
  delivery_address_snapshot:string;payment_terms_snapshot:string;note:string}
type LineDraft={item_id:string;description_snapshot:string;unit_id:string;ordered_qty:string;unit_price:string;due_date:string;
  preferred_warehouse_id:string;supplier_offer_id:string}
type Line=Audit&LineDraft&{purchase_order_id:string;line_no:number;base_factor_snapshot:string;line_amount:string;ordered_qty_base:string}
type Order=Audit&HeaderDraft&{approval_state:string|null;approved_at:string|null;approved_by:string|null;total_amount:string;line_count:number;lines:Line[]}
type Feedback={error?:string;notice?:string;id?:string}
type Editor={id:string|null;key:string;draft:LineDraft}
const blankHeader=():HeaderDraft=>({document_no:'',supplier_id:'',supplier_name_snapshot:'',currency_id:'',order_date:'',delivery_address_snapshot:'',payment_terms_snapshot:'',note:''})
const blankLine=():LineDraft=>({item_id:'',description_snapshot:'',unit_id:'',ordered_qty:'',unit_price:'',due_date:'',preferred_warehouse_id:'',supplier_offer_id:''})
const headerDraft=(row:Order):HeaderDraft=>Object.fromEntries(Object.keys(blankHeader()).map(k=>[k,row[k as keyof HeaderDraft]??''])) as HeaderDraft
const lineDraft=(row:Line):LineDraft=>Object.fromEntries(Object.keys(blankLine()).map(k=>[k,row[k as keyof LineDraft]??''])) as LineDraft

export function PurchaseOrders({onConnection}:{onConnection:(online:boolean)=>void}){
  const [orders,setOrders]=useState<Order[]>([]),[selected,setSelected]=useState<Order|null>(null)
  const [suppliers,setSuppliers]=useState<Supplier[]>([]),[materials,setMaterials]=useState<Material[]>([])
  const [currencies,setCurrencies]=useState<Ref[]>([]),[units,setUnits]=useState<Ref[]>([]),[warehouses,setWarehouses]=useState<Ref[]>([]),[offers,setOffers]=useState<Offer[]>([])
  const [draft,setDraft]=useState<HeaderDraft>(blankHeader),[headerEditing,setHeaderEditing]=useState(true)
  const [headerKey,setHeaderKey]=useState(()=>crypto.randomUUID()),[editor,setEditor]=useState<Editor|null>(null)
  const [query,setQuery]=useState(''),[applied,setApplied]=useState(''),[busy,setBusy]=useState(false)
  const [headerFeedback,setHeaderFeedback]=useState<Feedback>({}),[lineFeedback,setLineFeedback]=useState<Feedback>({}),[suggestion,setSuggestion]=useState<Feedback>({})
  const [listError,setListError]=useState(''),[openError,setOpenError]=useState<{id:string;message:string}|null>(null)
  const refName=(refs:Ref[],id:string)=>{const row=refs.find(r=>r.id===id);return row?row.code+' · '+row.name:'—'}
  async function loadList(q=applied){setOrders(await api<Order[]>('/purchase-orders?q='+encodeURIComponent(q)));setListError('');onConnection(true)}
  async function loadRefs(){
    const [ss,ms,cs,us,ws,os]=await Promise.all([api<Supplier[]>('/suppliers'),api<Material[]>('/raw-materials'),api<Ref[]>('/currencies'),api<Ref[]>('/units'),api<Ref[]>('/warehouses'),api<Offer[]>('/supplier-offers')])
    setSuppliers(ss);setMaterials(ms);setCurrencies(cs);setUnits(us);setWarehouses(ws);setOffers(os)
  }
  useEffect(()=>{Promise.all([loadList(''),loadRefs()]).then(()=>{const id=new URLSearchParams(window.location.hash.split('?')[1]||'').get('record');if(id)return open(id)}).catch(e=>{setListError(e.message);onConnection(false)})},[])
  function clearFeedback(){setHeaderFeedback({});setLineFeedback({});setSuggestion({});setOpenError(null)}
  function newOrder(){setSelected(null);setDraft(blankHeader());setHeaderKey(crypto.randomUUID());setHeaderEditing(true);setEditor(null);clearFeedback();window.history.replaceState(null,"","#purchase-orders")}
  async function open(id:string){
    clearFeedback()
    try{const row=await api<Order>('/purchase-orders/'+id);await loadRefs();setSelected(row);setDraft(headerDraft(row));setHeaderEditing(false);setEditor(null);window.history.replaceState(null,"","#purchase-orders?record="+row.id)}
    catch(e){setOpenError({id,message:(e as Error).message})}
  }
  async function refreshOrder(id:string){const row=await api<Order>('/purchase-orders/'+id);setSelected(row);setDraft(headerDraft(row));await loadList();return row}
  async function search(e:FormEvent){e.preventDefault();setApplied(query);try{await loadList(query)}catch(error){setListError((error as Error).message)}}
  function chooseSupplier(id:string){const row=suppliers.find(s=>s.id===id);setDraft(d=>({...d,supplier_id:id,supplier_name_snapshot:row?.name||'',currency_id:row?.currency_id||'',payment_terms_snapshot:row?.payment_terms||''}))}
  function cancelHeader(){if(selected){setDraft(headerDraft(selected));setHeaderEditing(false);clearFeedback()}else newOrder()}
  async function saveHeader(e:FormEvent){
    e.preventDefault();setBusy(true);setHeaderFeedback({})
    const body={...draft,delivery_address_snapshot:draft.delivery_address_snapshot||null,payment_terms_snapshot:draft.payment_terms_snapshot||null,note:draft.note||null,
      ...(selected?{row_version:selected.row_version}:{request_key:headerKey})}
    try{const row=await api<Order>('/purchase-orders'+(selected?'/'+selected.id:''),{method:selected?'PUT':'POST',body:JSON.stringify(body)})
      setSelected(row);setDraft(headerDraft(row));setHeaderEditing(false);window.history.replaceState(null,"","#purchase-orders?record="+row.id);await loadList();setHeaderFeedback({notice:selected?'表頭變更已儲存。':'採購單已新增。'})
    }catch(error){setHeaderFeedback({error:(error as Error).message})}finally{setBusy(false)}
  }
  async function removeHeader(){
    if(!selected||!window.confirm('確定刪除此採購單？有明細或其他引用的單據將受到保護。'))return
    setBusy(true);setHeaderFeedback({})
    try{await api('/purchase-orders/'+selected.id+'?row_version='+selected.row_version,{method:'DELETE'});newOrder();await loadList();setHeaderFeedback({notice:'採購單已刪除。'})}
    catch(error){setHeaderFeedback({error:(error as Error).message})}finally{setBusy(false)}
  }
  function editLine(row?:Line){setEditor({id:row?.id||null,key:crypto.randomUUID(),draft:row?lineDraft(row):blankLine()});setLineFeedback({});setHeaderFeedback({});setSuggestion({})}
  function changeLine(key:keyof LineDraft,value:string){setEditor(e=>e?{...e,draft:{...e.draft,[key]:value}}:null)}
  function chooseMaterial(id:string){const row=materials.find(m=>m.id===id);setEditor(e=>e?{...e,draft:{...e.draft,item_id:id,unit_id:row?.base_unit_id||'',description_snapshot:row?row.name+(row.specification?' · '+row.specification:''):'',supplier_offer_id:''}}:null);setSuggestion({})}
  async function suggestPrice(){
    if(!editor?.draft.supplier_offer_id||!selected)return
    setBusy(true);setSuggestion({})
    try{const offer=await api<Offer>('/supplier-offers/'+editor.draft.supplier_offer_id)
      if(offer.supplier_id!==selected.supplier_id||offer.item_id!==editor.draft.item_id)throw new Error('供料設定已變更，請重新選擇。')
      changeLine('unit_price',offer.unit_price);setSuggestion({notice:'參考單價已帶入；儲存明細後才會更新本單。'})
    }catch(error){setSuggestion({error:(error as Error).message})}finally{setBusy(false)}
  }
  async function saveLine(e:FormEvent){
    e.preventDefault();if(!selected||!editor)return
    setBusy(true);setLineFeedback({});setHeaderFeedback({})
    const editing=editor.id?selected.lines.find(l=>l.id===editor.id):null
    const body={...editor.draft,supplier_offer_id:editor.draft.supplier_offer_id||null,order_version:selected.row_version,
      ...(editing?{row_version:editing.row_version}:{request_key:editor.key})}
    try{const row=await api<Line>('/purchase-orders/'+selected.id+'/lines'+(editing?'/'+editing.id:''),{method:editing?'PUT':'POST',body:JSON.stringify(body)})
      await refreshOrder(selected.id);setEditor(null);setSuggestion({});setLineFeedback({id:row.id,notice:editing?'明細變更已儲存。':'明細已新增。'})
    }catch(error){setLineFeedback({id:editor.id||'new',error:(error as Error).message})}finally{setBusy(false)}
  }
  async function removeLine(row:Line){
    if(!selected||!window.confirm('確定刪除此筆未被引用的採購明細？'))return
    setBusy(true);setLineFeedback({})
    try{await api('/purchase-orders/'+selected.id+'/lines/'+row.id+'?row_version='+row.row_version+'&order_version='+selected.row_version,{method:'DELETE'})
      await refreshOrder(selected.id);setEditor(null);setSuggestion({});setLineFeedback({id:'new',notice:'明細已刪除，總額已更新。'})
    }catch(error){setLineFeedback({id:row.id,error:(error as Error).message})}finally{setBusy(false)}
  }
  const reload=selected?()=>{void open(selected.id)}:undefined
  const headerActions=(editing:boolean)=><div className="form-actions" data-feedback-group>
    {selected&&<DocumentApproval key={selected.id} type="PURCHASE_ORDER" id={selected.id} version={selected.row_version} blocked={busy||editing||!!editor}/>}
    <ActionFeedback {...headerFeedback} onReload={reload} busy={busy}/>
    {editing?<button className="button primary" type="submit" disabled={busy}>儲存表頭</button>:<button className="button primary" type="button" disabled={busy||!!editor} onClick={()=>{setDraft(headerDraft(selected!));setHeaderEditing(true);clearFeedback()}}>編輯表頭</button>}
    {selected&&<button className="button danger" type="button" disabled={busy||!!editor} onClick={removeHeader}>刪除採購單</button>}
    {editing&&<button className="button" type="button" disabled={busy} onClick={cancelHeader}>取消</button>}
  </div>
  const renderLineEditor=()=>{
    if(!editor||!selected)return null
    const d=editor.draft,row=editor.id?selected.lines.find(l=>l.id===editor.id):null
    const options=offers.filter(o=>o.supplier_id===selected.supplier_id&&o.item_id===d.item_id)
    return <form className="po-line-editor" onSubmit={saveLine} aria-label={row?'編輯採購明細':'新增採購明細'}>
      <div className="form-body po-fields">
        <div><label htmlFor="po-material">原物料 *</label><select id="po-material" required value={d.item_id} onChange={e=>chooseMaterial(e.target.value)}><option value="">請選擇原物料</option>{materials.map(m=><option key={m.id} value={m.id}>{m.code} · {m.name}</option>)}</select></div>
        <div><label htmlFor="po-unit">下單單位 *</label><select id="po-unit" required value={d.unit_id} onChange={e=>changeLine('unit_id',e.target.value)}><option value="">請先選擇原物料</option>{units.filter(u=>u.id===materials.find(m=>m.id===d.item_id)?.base_unit_id).map(u=><option key={u.id} value={u.id}>{u.code} · {u.name}</option>)}</select></div>
        <div className="po-wide"><label htmlFor="po-description">品名規格快照 *</label><textarea id="po-description" required value={d.description_snapshot} onChange={e=>changeLine('description_snapshot',e.target.value)}/></div>
        <div><label htmlFor="po-qty">訂購數量 *</label><input id="po-qty" type="text" inputMode="decimal" required value={d.ordered_qty} onChange={e=>changeLine('ordered_qty',e.target.value)}/></div>
        <div><label htmlFor="po-price">採購單價 *</label><input id="po-price" type="text" inputMode="decimal" required value={d.unit_price} onChange={e=>changeLine('unit_price',e.target.value)}/></div>
        <div><label htmlFor="po-due">預計到貨日 *</label><input id="po-due" type="date" required value={d.due_date} onChange={e=>changeLine('due_date',e.target.value)}/></div>
        <div><label htmlFor="po-warehouse">預計收貨倉 *</label><select id="po-warehouse" required value={d.preferred_warehouse_id} onChange={e=>changeLine('preferred_warehouse_id',e.target.value)}><option value="">請選擇倉庫</option>{warehouses.map(w=><option key={w.id} value={w.id}>{w.code} · {w.name}</option>)}</select></div>
        <div className="po-wide"><label htmlFor="po-offer">報價依據（選填）</label><select id="po-offer" value={d.supplier_offer_id} onChange={e=>{changeLine('supplier_offer_id',e.target.value);setSuggestion({})}}><option value="">不引用供料設定</option>{options.map(o=><option key={o.id} value={o.id}>{o.unit_price}{o.is_active?'':'（停用）'}</option>)}</select>
          <div className="po-suggestion" data-feedback-group><button className="button" type="button" disabled={busy||!d.supplier_offer_id} onClick={suggestPrice}>帶入參考單價</button><ActionFeedback {...suggestion}/></div></div>
        <p className="field-note po-wide">僅支援原料基本單位。單價可自行輸入，儲存後由系統精確計算金額；修改供料設定不會回改本單。</p>
      </div><div className="form-actions" data-feedback-group>
        <ActionFeedback {...(lineFeedback.id===(editor.id||'new')?lineFeedback:{})} onReload={reload} busy={busy}/>
        <button className="button primary" type="submit" disabled={busy}>儲存明細</button>
        {row&&<button className="button danger" type="button" disabled={busy} onClick={()=>removeLine(row)}>刪除此明細</button>}
        <button className="button" type="button" disabled={busy} onClick={()=>{setEditor(null);setLineFeedback({});setSuggestion({})}}>取消</button>
      </div>
    </form>
  }
  return <>
    <div className="page-heading"><div><div className="eyebrow">PURCHASING / PURCHASE ORDERS</div><h1>採購訂單管理</h1><p>逐筆維護採購表頭與明細，查看各筆金額與整單總額。</p></div><button className="button primary new-button" disabled={busy} onClick={newOrder}>＋ 新增採購單</button></div>
    <div className="page-meta"><span><span className="teal-dot"/>採購管理</span><span>表頭 / 明細 / 總額</span><span className="meta-right">可另至採購入庫管理驗收入庫</span></div>
    <div className="po-layout">
      <section className="panel po-list" aria-label="採購單清單"><div className="panel-title"><h2>採購單清單 <span className="count">{orders.length}</span></h2></div>
        <div className="search-area" data-feedback-group><form className="search-bar" onSubmit={search}><label className="sr-only" htmlFor="po-search">搜尋採購單</label><input id="po-search" value={query} onChange={e=>setQuery(e.target.value)} placeholder="搜尋單號或供應商"/><button type="submit">搜尋</button></form><ActionFeedback error={listError}/></div>
        {orders.map(row=><div className={'po-list-row '+(selected?.id===row.id?'current-row':'')} key={row.id} data-feedback-group>
          <button className="record-link" disabled={busy} onClick={()=>open(row.id)} aria-label={'開啟採購單 '+row.document_no}><span>{row.document_no} · {row.order_date}</span><strong>{row.supplier_name_snapshot}</strong><span className="po-list-total">{row.total_amount} · {row.line_count} 筆明細</span></button><ActionFeedback error={openError?.id===row.id?openError.message:''}/></div>)}
        {!orders.length&&<div className="empty-state"><h3>{applied?'沒有符合的採購單':'尚無採購單'}</h3><p>使用右側表單新增採購單。</p></div>}
      </section>
      <div className="po-detail">
        <section className="panel po-header" aria-label="採購表頭"><div className="panel-title"><h2>{selected?selected.document_no:'新增採購單'}</h2><span className="form-kind">核准流程待核定</span></div>
          {headerEditing?<form onSubmit={saveHeader}><div className="form-body po-fields">
            <div><label htmlFor="po-document">採購單號 *</label><input id="po-document" required value={draft.document_no} onChange={e=>setDraft(d=>({...d,document_no:e.target.value}))}/></div>
            <div><label htmlFor="po-date">採購日期 *</label><input id="po-date" type="date" required value={draft.order_date} onChange={e=>setDraft(d=>({...d,order_date:e.target.value}))}/></div>
            <div><label htmlFor="po-supplier">供應商 *</label><select id="po-supplier" required value={draft.supplier_id} onChange={e=>chooseSupplier(e.target.value)}><option value="">請選擇供應商</option>{suppliers.map(s=><option key={s.id} value={s.id}>{s.code} · {s.name}</option>)}</select></div>
            <div><label htmlFor="po-currency">幣別 *</label><select id="po-currency" required value={draft.currency_id} onChange={e=>setDraft(d=>({...d,currency_id:e.target.value}))}><option value="">請選擇幣別</option>{currencies.map(c=><option key={c.id} value={c.id}>{c.code} · {c.name}</option>)}</select></div>
            <div className="po-wide"><label htmlFor="po-supplier-name">供應商名稱快照 *</label><input id="po-supplier-name" required value={draft.supplier_name_snapshot} onChange={e=>setDraft(d=>({...d,supplier_name_snapshot:e.target.value}))}/></div>
            <div><label htmlFor="po-delivery">交貨地址快照</label><textarea id="po-delivery" value={draft.delivery_address_snapshot} onChange={e=>setDraft(d=>({...d,delivery_address_snapshot:e.target.value}))}/></div>
            <div><label htmlFor="po-payment">付款條件快照</label><textarea id="po-payment" value={draft.payment_terms_snapshot} onChange={e=>setDraft(d=>({...d,payment_terms_snapshot:e.target.value}))}/></div>
            <div className="po-wide"><label htmlFor="po-note">備註</label><textarea id="po-note" value={draft.note} onChange={e=>setDraft(d=>({...d,note:e.target.value}))}/></div>
          </div>{headerActions(true)}</form>:selected&&<><dl className="po-header-summary"><dt>採購日期</dt><dd data-testid="po-order-date">{selected.order_date}</dd><dt>供應商快照</dt><dd>{selected.supplier_name_snapshot}</dd><dt>幣別</dt><dd>{refName(currencies,selected.currency_id)}</dd><dt>交貨地址</dt><dd>{selected.delivery_address_snapshot||'—'}</dd><dt>付款條件</dt><dd>{selected.payment_terms_snapshot||'—'}</dd><dt>備註</dt><dd data-testid="po-note-value">{selected.note||'—'}</dd></dl>{headerActions(false)}</>}
        </section>
        {selected&&<section className="panel po-lines" aria-label="採購明細"><div className="panel-title"><h2>採購明細 <span className="count">{selected.line_count}</span></h2><strong className="po-total">總額 <span data-testid="po-total">{selected.total_amount}</span></strong></div>
          <p className="field-note po-policy">金額未做自動取位；稅務、核准與幣別取位規則待核定。每次僅編輯一筆明細。</p>
          {selected.lines.map(row=><article className="po-line" key={row.id} data-testid={'po-line-'+row.line_no}>
            <div className="po-line-heading"><strong>明細 {row.line_no} · {refName(materials,row.item_id)}</strong><span>金額 <b data-testid={'po-amount-'+row.line_no}>{row.line_amount}</b></span></div>
            <dl className="po-line-values"><dt>品名規格</dt><dd>{row.description_snapshot}</dd><dt>數量 × 單價</dt><dd>{row.ordered_qty} × {row.unit_price}</dd><dt>下單單位</dt><dd>{refName(units,row.unit_id)}</dd><dt>預計到貨日</dt><dd>{row.due_date}</dd><dt>預計收貨倉</dt><dd>{refName(warehouses,row.preferred_warehouse_id)}</dd></dl>
            {editor?.id===row.id?renderLineEditor():<div className="form-actions" data-feedback-group><ActionFeedback {...(lineFeedback.id===row.id?lineFeedback:{})} onReload={reload} busy={busy}/><button className="button" disabled={busy||headerEditing||!!editor} onClick={()=>editLine(row)}>編輯明細 {row.line_no}</button><button className="button danger" disabled={busy||headerEditing||!!editor} onClick={()=>removeLine(row)}>刪除明細 {row.line_no}</button></div>}
          </article>)}
          {!selected.lines.length&&<div className="po-no-lines">此採購單尚無明細，總額為 0。</div>}
          {editor&&!editor.id?renderLineEditor():<div className="form-actions" data-feedback-group><ActionFeedback {...(lineFeedback.id==='new'?lineFeedback:{})}/><button className="button primary" disabled={busy||headerEditing||!!editor} onClick={()=>editLine()}>＋ 新增明細</button></div>}
          <details className="audit po-audit"><summary>系統紀錄 · 採購單版次 {selected.row_version}</summary><dl><dt>UUID</dt><dd data-testid="po-uuid">{selected.id}</dd><dt>建立時間</dt><dd>{selected.created_at}</dd><dt>最後更新</dt><dd>{selected.updated_at}</dd><dt>建立者</dt><dd>本機示範操作員（未驗證）</dd></dl></details>
        </section>}
      </div>
    </div>
    <footer className="page-footer">TIGER ERP <span>本機示範版 · 採購訂單管理</span><span>付款與正式核准尚待核定</span></footer>
  </>
}
