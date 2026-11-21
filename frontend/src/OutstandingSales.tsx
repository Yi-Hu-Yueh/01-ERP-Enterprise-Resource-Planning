import {useEffect,useState,type FormEvent} from 'react'
import {api} from './api'
import {ActionFeedback} from './ActionFeedback'

type Ref={id:string;code:string;name:string}
type Totals={unit_id:string;unit_code:string;unit_name:string;line_count:number;ordered_qty_base:string;gross_shipped_qty_base:string;outstanding_qty_base:string;finalized_returned_qty_base:string}
type ReturnSource={id:string;document_no:string;quantity:string;finalization_state:string}
type Shipment={id:string;document_no:string;quantity:string;business_date:string;returns:ReturnSource[]}
type Line=Omit<Totals,'line_count'>&{id:string;line_no:number;product_code:string;product_name:string;description_snapshot:string;unit_price:string;line_amount:string;matches_product_filter:boolean;diagnostic:string|null;shipments:Shipment[]}
type Order={id:string;document_no:string;order_date:string;state:string;customer_code:string;customer_name:string;total_amount:string;line_count:number;has_outstanding:boolean;quantity_totals:Totals[];lines:Line[]}
type Result={total_orders:number;total_lines:number;quantity_totals:Totals[];orders:Order[]}
type Filters={document_no:string;customer_id:string;product_id:string;date_from:string;date_to:string;outstanding_only:boolean}
const blank=():Filters=>({document_no:'',customer_id:'',product_id:'',date_from:'',date_to:'',outstanding_only:false})
const params=()=>new URLSearchParams(location.hash.split('?')[1]||'')
function initial(){const p=params(),f=blank();for(const k of ['document_no','customer_id','product_id','date_from','date_to'] as const)f[k]=p.get(k)||'';f.outstanding_only=p.get('outstanding_only')==='true';return f}
function query(f:Filters){const p=new URLSearchParams();for(const [k,v] of Object.entries(f))if(v)p.set(k,String(v));return p}
function remember(f:Filters,id:string|null){const p=query(f);if(id)p.set('order',id);history.replaceState(null,'','#outstanding-sales'+(p.size?'?'+p.toString():''))}
function Quantities({totals,prefix}:{totals:Totals[];prefix:string}){return <div className="inquiry-quantities">{totals.map(t=><dl className="po-line-values" key={t.unit_id} data-testid={prefix+'-unit'}><dt>基本單位</dt><dd>{t.unit_code} · {t.unit_name}</dd><dt>訂購數量</dt><dd data-testid={prefix+'-ordered'}>{t.ordered_qty_base}</dd><dt>累計已出貨（毛額）</dt><dd data-testid={prefix+'-shipped'}>{t.gross_shipped_qty_base}</dd><dt>剩餘未出貨</dt><dd data-testid={prefix+'-outstanding'} className={t.outstanding_qty_base.startsWith('-')?'inquiry-negative':''}>{t.outstanding_qty_base}</dd><dt>已完成客戶退貨（獨立列示）</dt><dd data-testid={prefix+'-returned'}>{t.finalized_returned_qty_base}</dd></dl>)}</div>}

export function OutstandingSales({onConnection}:{onConnection:(online:boolean)=>void}){
 const [filters,setFilters]=useState<Filters>(initial),[applied,setApplied]=useState<Filters>(initial)
 const [result,setResult]=useState<Result|null>(null),[customers,setCustomers]=useState<Ref[]>([]),[products,setProducts]=useState<Ref[]>([])
 const [open,setOpen]=useState<string|null>(()=>params().get('order')),[busy,setBusy]=useState(false),[error,setError]=useState('')
 async function load(f:Filters=applied,expanded:string|null=open){setBusy(true);setError('');try{const r=await api<Result>('/outstanding-sales?'+query(f));setResult(r);setApplied({...f});remember(f,expanded);onConnection(true)}catch(e){setError((e as Error).message);onConnection(false)}finally{setBusy(false)}}
 useEffect(()=>{void load(initial());Promise.all([api<Ref[]>('/customers'),api<Ref[]>('/finished-goods')]).then(([c,p])=>{setCustomers(c);setProducts(p)}).catch(e=>setError(e.message))},[])
 function change<K extends keyof Filters>(k:K,v:Filters[K]){setFilters(f=>({...f,[k]:v}))}
 async function search(e:FormEvent){e.preventDefault();await load(filters)}
 function clear(){const f=blank();setFilters(f);setOpen(null);remember(f,null);void load(f,null)}
 function expand(id:string){const next=open===id?null:id;setOpen(next);remember(applied,next)}
 return <>
  <div className="page-heading"><div><div className="eyebrow">SALES / OUTSTANDING INQUIRY</div><h1>未出貨訂單查詢</h1><p>依真實銷售明細與已過帳出貨查詢；已完成退貨獨立顯示，不重開需求。</p></div></div>
  <div className="page-meta"><span><span className="teal-dot"/>唯讀查詢</span><span>剩餘未出貨 = 訂購 − 累計已出貨毛額</span></div>
  <section className="panel" aria-label="銷售查詢條件"><div className="panel-title"><h2>查詢條件</h2></div><form onSubmit={search}><div className="form-body po-fields">
   <div className="po-wide"><label htmlFor="os-number">銷售訂單號碼</label><input id="os-number" value={filters.document_no} onChange={e=>change('document_no',e.target.value)}/></div>
   <div><label htmlFor="os-customer">客戶</label><select id="os-customer" value={filters.customer_id} onChange={e=>change('customer_id',e.target.value)}><option value="">全部客戶</option>{customers.map(c=><option key={c.id} value={c.id}>{c.code} · {c.name}</option>)}</select></div>
   <div><label htmlFor="os-product">包含成品（保留整張訂單全部明細）</label><select id="os-product" value={filters.product_id} onChange={e=>change('product_id',e.target.value)}><option value="">全部成品</option>{products.map(p=><option key={p.id} value={p.id}>{p.code} · {p.name}</option>)}</select></div>
   <div><label htmlFor="os-from">銷售日期起</label><input id="os-from" type="date" value={filters.date_from} onChange={e=>change('date_from',e.target.value)}/></div><div><label htmlFor="os-to">銷售日期迄</label><input id="os-to" type="date" value={filters.date_to} onChange={e=>change('date_to',e.target.value)}/></div>
   <label className="check-control po-wide"><input type="checkbox" checked={filters.outstanding_only} onChange={e=>change('outstanding_only',e.target.checked)}/>僅顯示尚有未出貨明細的訂單</label>
  </div><div className="form-actions" data-feedback-group><ActionFeedback error={error} busy={busy}/><button className="button primary" type="submit" disabled={busy}>查詢</button><button className="button" type="button" disabled={busy} onClick={clear}>清除條件</button><button className="button" type="button" disabled={busy} onClick={()=>void load()}>重新整理</button></div></form></section>
  <p className="field-note po-policy">篩選選取整張訂單，摘要與金額均保留全部明細；符合成品篩選的明細另行標示。數量只依同一基本單位分組。退貨不扣減出貨毛額，也不增加剩餘未出貨；庫存不代表訂單未交量。</p>
  {error&&result&&<p className="field-note">查詢失敗，下方保留上次成功結果。</p>}
  {result&&<><section className="panel inquiry-summary" aria-label="全部符合銷售結果摘要"><div className="panel-title"><h2>符合結果 <span data-testid="os-order-count">{result.total_orders}</span> 張 / {result.total_lines} 筆明細</h2></div><Quantities totals={result.quantity_totals} prefix="os-summary"/></section>
   <div className="inquiry-orders">{result.orders.map(o=><section className="panel" key={o.id} data-testid={'os-order-'+o.id}><div className="panel-title"><h2>{o.document_no}</h2><span>{o.order_date} · {o.state}</span></div><div className="po-line-heading"><strong>{o.customer_code} · {o.customer_name}</strong><span>銷售總金額 <b data-testid="os-sum">{o.total_amount}</b> · {o.line_count} 筆明細</span></div><Quantities totals={o.quantity_totals} prefix="os-order"/>
    <div className="form-actions"><button className="button" type="button" aria-expanded={open===o.id} onClick={()=>expand(o.id)}>{open===o.id?'收合明細':'查看全部明細'} · {o.document_no}</button><a className="button" href={'#sales-orders?record='+o.id}>查看原銷售訂單 · {o.document_no}</a></div>
    {open===o.id&&<div aria-label={o.document_no+' 銷售明細'}>{!o.lines.length&&<p className="empty-state">此訂單尚無明細。</p>}{o.lines.map(l=><article className="po-line" key={l.id} data-testid={'os-line-'+l.line_no}><div className="po-line-heading"><strong>明細 {l.line_no} · {l.product_code} · {l.product_name}</strong><span>原金額 {l.line_amount}</span></div>{applied.product_id&&l.matches_product_filter&&<p className="field-note">符合成品篩選</p>}<p className="field-note">明細識別：{l.id}</p><dl className="po-line-values"><dt>品名規格</dt><dd>{l.description_snapshot}</dd><dt>原單價</dt><dd>{l.unit_price}</dd><dt>基本單位</dt><dd>{l.unit_code} · {l.unit_name}</dd><dt>訂購數量</dt><dd data-testid="os-line-ordered">{l.ordered_qty_base}</dd><dt>累計已出貨（毛額）</dt><dd data-testid="os-line-shipped">{l.gross_shipped_qty_base}</dd><dt>剩餘未出貨</dt><dd data-testid="os-line-outstanding">{l.outstanding_qty_base}</dd><dt>已完成客戶退貨（不重開需求）</dt><dd data-testid="os-line-returned">{l.finalized_returned_qty_base}</dd></dl>{l.diagnostic&&<p role="alert" className="inquiry-negative">{l.diagnostic}</p>}
     {l.shipments.map(s=><div className="po-policy" key={s.id}><a href={'#sales-shipments?record='+s.id}>原出貨 {s.document_no}</a> · {s.quantity} · {s.business_date} · {s.id}{s.returns.map(r=><p key={r.id}><a href={'#customer-returns?record='+r.id}>已完成客戶退貨 {r.document_no}</a> · {r.quantity} · {r.finalization_state} · {r.id}</p>)}</div>)}
    </article>)}</div>}
   </section>)}</div>{!result.orders.length&&<p className="empty-state">沒有符合的銷售訂單。</p>}</>}
 </>
}
