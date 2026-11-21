import {DocumentApproval} from './DocumentApproval'
import {useEffect,useState,type FormEvent} from 'react'
import {api} from './api'
import {ActionFeedback} from './ActionFeedback'

type Ref={id:string;code:string;name:string}
type Product=Ref&{base_unit_id:string;specification:string|null}
type HeaderDraft={document_no:string;customer_id:string;order_date:string;note:string}
type LineDraft={item_id:string;unit_id:string;description_snapshot:string;ordered_qty:string;unit_price:string}
type Line=LineDraft&{id:string;sales_order_id:string;line_no:number;row_version:number;line_amount:string}
type Order=HeaderDraft&{id:string;customer_code:string;customer_name:string;state:string;row_version:number;lines:Line[];line_count:number;total_amount:string}
type Feedback={error?:string;notice?:string;id?:string}
type Editor={row:Line|null;key:string;draft:LineDraft}
const emptyHeader=():HeaderDraft=>({document_no:'',customer_id:'',order_date:'',note:''})
const emptyLine=():LineDraft=>({item_id:'',unit_id:'',description_snapshot:'',ordered_qty:'',unit_price:''})
const headerDraft=(o:Order):HeaderDraft=>({document_no:o.document_no,customer_id:o.customer_id,order_date:o.order_date,note:o.note||''})
const lineDraft=(l:Line):LineDraft=>({item_id:l.item_id,unit_id:l.unit_id,description_snapshot:l.description_snapshot,ordered_qty:l.ordered_qty,unit_price:l.unit_price})

export function SalesOrders({onConnection}:{onConnection:(online:boolean)=>void}){
  const [orders,setOrders]=useState<Order[]>([]),[selected,setSelected]=useState<Order|null>(null)
  const [customers,setCustomers]=useState<Ref[]>([]),[products,setProducts]=useState<Product[]>([]),[units,setUnits]=useState<Ref[]>([])
  const [draft,setDraft]=useState<HeaderDraft>(emptyHeader),[editing,setEditing]=useState(true),[key,setKey]=useState(()=>crypto.randomUUID())
  const [editor,setEditor]=useState<Editor|null>(null),[busy,setBusy]=useState(false),[query,setQuery]=useState(''),[applied,setApplied]=useState('')
  const [headerFeedback,setHeaderFeedback]=useState<Feedback>({}),[lineFeedback,setLineFeedback]=useState<Feedback>({}),[listError,setListError]=useState('')
  const [openError,setOpenError]=useState<Feedback>({})
  const name=(refs:Ref[],id:string)=>{const r=refs.find(x=>x.id===id);return r?r.code+' · '+r.name:'—'}
  async function loadList(q=applied){setOrders(await api<Order[]>('/sales-orders?q='+encodeURIComponent(q)));setListError('');onConnection(true)}
  async function loadRefs(){const [c,p,u]=await Promise.all([api<Ref[]>('/customers'),api<Product[]>('/finished-goods'),api<Ref[]>('/units')]);setCustomers(c);setProducts(p);setUnits(u)}
  function clear(){setHeaderFeedback({});setLineFeedback({});setOpenError({})}
  function remember(id?:string){window.history.replaceState(null,'','#sales-orders'+(id?'?record='+id:''))}
  function newOrder(){setSelected(null);setDraft(emptyHeader());setEditing(true);setEditor(null);setKey(crypto.randomUUID());clear();remember()}
  async function open(id:string){clear();try{const row=await api<Order>('/sales-orders/'+id);await loadRefs();setSelected(row);setDraft(headerDraft(row));setEditing(false);setEditor(null);remember(row.id)}catch(e){setOpenError({id,error:(e as Error).message})}}
  async function refresh(id:string){const row=await api<Order>('/sales-orders/'+id);setSelected(row);setDraft(headerDraft(row));await loadList();return row}
  useEffect(()=>{Promise.all([loadList(''),loadRefs()]).then(()=>{const id=new URLSearchParams(window.location.hash.split('?')[1]||'').get('record');if(id)return open(id)}).catch(e=>{setListError(e.message);onConnection(false)})},[])
  useEffect(()=>{function route(){if(window.location.hash.split('?')[0]!=='#sales-orders')return;const id=new URLSearchParams(window.location.hash.split('?')[1]||'').get('record');if(id)void open(id);else newOrder()}window.addEventListener('hashchange',route);return()=>window.removeEventListener('hashchange',route)},[])
  function cancelHeader(){clear();if(selected){setDraft(headerDraft(selected));setEditing(false)}else newOrder()}
  function cancelLine(){setEditor(null);setLineFeedback({})}
  useEffect(()=>{function escape(e:KeyboardEvent){if(e.key==='Escape'&&!busy){if(editor)cancelLine();else if(editing)cancelHeader()}}window.addEventListener('keydown',escape);return()=>window.removeEventListener('keydown',escape)},[editor,editing,busy,selected])
  async function saveHeader(e:FormEvent){e.preventDefault();setBusy(true);setHeaderFeedback({});try{
    const row=await api<Order>('/sales-orders'+(selected?'/'+selected.id:''),{method:selected?'PUT':'POST',body:JSON.stringify({...draft,note:draft.note||null,...(selected?{row_version:selected.row_version}:{request_key:key})})})
    setSelected(row);setDraft(headerDraft(row));setEditing(false);remember(row.id);await loadList();setHeaderFeedback({notice:'銷售表頭已儲存。'})
  }catch(e){setHeaderFeedback({error:(e as Error).message})}finally{setBusy(false)}}
  async function removeHeader(){if(!selected||!window.confirm('確定刪除此銷售單？有明細或下游引用時無法刪除。'))return;setBusy(true);setHeaderFeedback({});try{await api('/sales-orders/'+selected.id+'?row_version='+selected.row_version,{method:'DELETE'});newOrder();await loadList();setHeaderFeedback({notice:'銷售單已刪除。'})}catch(e){setHeaderFeedback({error:(e as Error).message})}finally{setBusy(false)}}
  function editLine(row:Line|null=null){setEditor({row,key:crypto.randomUUID(),draft:row?lineDraft(row):emptyLine()});clear()}
  function changeLine(field:keyof LineDraft,value:string){setEditor(e=>e?{...e,draft:{...e.draft,[field]:value}}:null)}
  function chooseProduct(id:string){const p=products.find(x=>x.id===id);setEditor(e=>e?{...e,draft:{...e.draft,item_id:id,unit_id:p?.base_unit_id||'',description_snapshot:p?p.name+(p.specification?' · '+p.specification:''):''}}:null)}
  async function saveLine(e:FormEvent){e.preventDefault();if(!selected||!editor)return;setBusy(true);setLineFeedback({});try{
    const result=await api<Line>('/sales-orders/'+selected.id+'/lines'+(editor.row?'/'+editor.row.id:''),{method:editor.row?'PUT':'POST',body:JSON.stringify({...editor.draft,order_version:selected.row_version,...(editor.row?{row_version:editor.row.row_version}:{request_key:editor.key})})})
    await refresh(selected.id);setEditor(null);setLineFeedback({id:result.id,notice:'明細已儲存，銷售總金額已重新計算。'})
  }catch(e){setLineFeedback({id:editor.row?.id||'new',error:(e as Error).message})}finally{setBusy(false)}}
  async function removeLine(row:Line){if(!selected||!window.confirm('確定刪除此銷售明細？'))return;setBusy(true);setLineFeedback({});try{
    await api('/sales-orders/'+selected.id+'/lines/'+row.id+'?row_version='+row.row_version+'&order_version='+selected.row_version,{method:'DELETE'});await refresh(selected.id);setEditor(null);setLineFeedback({id:'new',notice:'明細已刪除，銷售總金額已重新計算。'})
  }catch(e){setLineFeedback({id:row.id,error:(e as Error).message})}finally{setBusy(false)}}
  async function reload(){if(!selected)return;setBusy(true);try{await refresh(selected.id);setEditing(false);setEditor(null);clear()}catch(e){if(editor)setLineFeedback({id:editor.row?.id||'new',error:(e as Error).message});else setHeaderFeedback({error:(e as Error).message})}finally{setBusy(false)}}
  const headerActions=(isEditing:boolean)=><div className="form-actions" data-feedback-group><ActionFeedback {...headerFeedback} onReload={reload} busy={busy}/>
    {selected&&<DocumentApproval key={selected.id} type="SALES_ORDER" id={selected.id} version={selected.row_version} blocked={busy||isEditing||!!editor}/>}
    {isEditing?<button className="button primary" type="submit" disabled={busy}>儲存變更</button>:<button className="button primary" type="button" disabled={busy||!!editor} onClick={()=>{clear();setEditing(true)}}>編輯表頭</button>}
    {selected&&<button className="button danger" type="button" disabled={busy||!!editor} onClick={removeHeader}>刪除銷售單</button>}
    {isEditing&&<button className="button" type="button" disabled={busy} onClick={cancelHeader}>取消</button>}
  </div>
  const lineEditor=()=>editor&&<form className="po-line-editor" aria-label={editor.row?'編輯銷售明細':'新增銷售明細'} onSubmit={saveLine}>
    <div className="form-body po-fields">
      <div><label htmlFor="so-product">成品 *</label><select id="so-product" required value={editor.draft.item_id} onChange={e=>chooseProduct(e.target.value)}><option value="">請選擇成品</option>{products.map(p=><option key={p.id} value={p.id}>{p.code} · {p.name}</option>)}</select></div>
      <div><label htmlFor="so-unit">基本單位</label><input id="so-unit" readOnly value={name(units,editor.draft.unit_id)}/></div>
      <div className="po-wide"><label htmlFor="so-description">品名規格</label><textarea id="so-description" required value={editor.draft.description_snapshot} onChange={e=>changeLine('description_snapshot',e.target.value)}/></div>
      <div><label htmlFor="so-qty">數量 *</label><input id="so-qty" type="text" inputMode="decimal" required value={editor.draft.ordered_qty} onChange={e=>changeLine('ordered_qty',e.target.value)}/></div>
      <div><label htmlFor="so-price">單價 *</label><input id="so-price" type="text" inputMode="decimal" required value={editor.draft.unit_price} onChange={e=>changeLine('unit_price',e.target.value)}/></div>
      <p className="field-note po-wide">儲存後顯示後端精確計算的明細金額與銷售總金額。每次只儲存一筆明細。</p>
    </div><div className="form-actions" data-feedback-group><ActionFeedback {...(lineFeedback.id===(editor.row?.id||'new')?lineFeedback:{})} onReload={reload} busy={busy}/>
      <button className="button primary" type="submit" disabled={busy}>儲存明細</button>{editor.row&&<button className="button danger" type="button" disabled={busy} onClick={()=>removeLine(editor.row!)}>刪除此明細</button>}
      <button className="button" type="button" disabled={busy} onClick={cancelLine}>取消</button>
    </div></form>
  return <>
    <div className="page-heading"><div><div className="eyebrow">SALES / SALES ORDERS</div><h1>銷售訂單管理</h1><p>逐筆維護客戶訂單與成品明細，查看精確金額。</p></div><button className="button primary new-button" disabled={busy} onClick={newOrder}>＋ 新增銷售單</button></div>
    <div className="page-meta"><span><span className="teal-dot"/>銷售管理</span><span>訂單記錄需求；實際出貨作業尚未實作。</span></div>
    <div className="po-layout"><section className="panel po-list" aria-label="銷售單清單"><div className="panel-title"><h2>銷售單清單 <span className="count">{orders.length}</span></h2></div>
      <div className="search-area" data-feedback-group><form className="search-bar" onSubmit={async e=>{e.preventDefault();setApplied(query);try{await loadList(query)}catch(x){setListError((x as Error).message)}}}><label className="sr-only" htmlFor="so-search">搜尋銷售單</label><input id="so-search" placeholder="搜尋單號或客戶" value={query} onChange={e=>setQuery(e.target.value)}/><button>搜尋</button></form><ActionFeedback error={listError}/></div>
      {orders.map(o=><div className={'po-list-row '+(selected?.id===o.id?'current-row':'')} key={o.id} data-feedback-group><button className="record-link" disabled={busy} onClick={()=>open(o.id)} aria-label={'開啟銷售單 '+o.document_no}><span>{o.document_no} · {o.order_date}</span><strong>{o.customer_code} · {o.customer_name}</strong><span>{o.total_amount} · {o.line_count} 筆明細</span></button><ActionFeedback {...(openError.id===o.id?openError:{})}/></div>)}
      {!orders.length&&<div className="empty-state"><h3>{applied?'沒有符合的銷售單':'尚無銷售單'}</h3><p>使用右側表單新增銷售單。</p></div>}
    </section><div className="po-detail"><section className="panel po-header" aria-label="銷售表頭"><div className="panel-title"><h2>{selected?selected.document_no:'新增銷售單'}</h2><span className="form-kind">DRAFT</span></div>
      {editing?<form onSubmit={saveHeader}><div className="form-body po-fields">
        <div><label htmlFor="so-number">銷售單號 *</label><input id="so-number" required value={draft.document_no} onChange={e=>setDraft(d=>({...d,document_no:e.target.value}))}/></div>
        <div><label htmlFor="so-date">銷售日期 *</label><input id="so-date" type="date" required value={draft.order_date} onChange={e=>setDraft(d=>({...d,order_date:e.target.value}))}/></div>
        <div className="po-wide"><label htmlFor="so-customer">客戶 *</label><select id="so-customer" required value={draft.customer_id} onChange={e=>setDraft(d=>({...d,customer_id:e.target.value}))}><option value="">請選擇客戶</option>{customers.map(c=><option key={c.id} value={c.id}>{c.code} · {c.name}</option>)}</select></div>
        <div className="po-wide"><label htmlFor="so-note">備註</label><textarea id="so-note" value={draft.note} onChange={e=>setDraft(d=>({...d,note:e.target.value}))}/></div>
      </div>{headerActions(true)}</form>:selected&&<><dl className="po-header-summary"><dt>客戶</dt><dd>{selected.customer_code} · {selected.customer_name}</dd><dt>銷售日期</dt><dd>{selected.order_date}</dd><dt>備註</dt><dd data-testid="so-note-value">{selected.note||'—'}</dd></dl>{headerActions(false)}</>}
    </section>{selected&&<section className="panel po-lines" aria-label="銷售明細"><div className="panel-title"><h2>銷售明細 <span className="count">{selected.line_count}</span></h2><strong className="po-total">銷售總金額 <span data-testid="so-total">{selected.total_amount}</span></strong></div>
      {selected.lines.map(l=><article className="po-line" key={l.id} data-testid={'so-line-'+l.line_no}><div className="po-line-heading"><strong>明細 {l.line_no} · {name(products,l.item_id)}</strong><span>明細金額 <b data-testid={'so-amount-'+l.line_no}>{l.line_amount}</b></span></div><dl className="po-line-values"><dt>品名規格</dt><dd>{l.description_snapshot}</dd><dt>數量 × 單價</dt><dd>{l.ordered_qty} × {l.unit_price}</dd><dt>基本單位</dt><dd>{name(units,l.unit_id)}</dd></dl>
        {editor?.row?.id===l.id?lineEditor():<div className="form-actions" data-feedback-group><ActionFeedback {...(lineFeedback.id===l.id?lineFeedback:{})} onReload={reload} busy={busy}/><button className="button" disabled={busy||editing||!!editor} onClick={()=>editLine(l)}>編輯明細 {l.line_no}</button><button className="button danger" disabled={busy||editing||!!editor} onClick={()=>removeLine(l)}>刪除明細 {l.line_no}</button></div>}
      </article>)}
      {!selected.lines.length&&<div className="po-no-lines">此銷售單尚無明細，總額為 0。</div>}
      {editor&&!editor.row?lineEditor():<div className="form-actions" data-feedback-group><ActionFeedback {...(lineFeedback.id==='new'?lineFeedback:{})}/><button className="button primary" disabled={busy||editing||!!editor} onClick={()=>editLine()}>＋ 新增明細</button></div>}
    </section>}</div></div>
  </>
}
