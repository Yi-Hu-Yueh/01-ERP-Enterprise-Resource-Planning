import {useEffect,useState,type FormEvent} from 'react'
import {api} from './api'
import {ActionFeedback} from './ActionFeedback'

type Ref={id:string;code:string;name:string}
type Totals={unit_id:string;unit_code:string;unit_name:string;ordered_qty_base:string;gross_received_qty_base:string;outstanding_qty_base:string;posted_return_qty_base:string;net_retained_qty_base:string;line_count:number}
type Receipt={receipt_id:string;document_no:string;business_date:string;receipt_line_id:string;receipt_line_no:number;quantity_base:string;posted_return_qty_base:string}
type Line=Omit<Totals,'line_count'>&{id:string;line_no:number;item_code:string;item_name:string;description_snapshot:string;ordered_qty:string;unit_price:string;line_amount:string;due_date:string;warehouse_code:string;warehouse_name:string;has_outstanding:boolean;receipts:Receipt[]}
type Order={id:string;document_no:string;order_date:string;supplier_code:string;supplier_name:string;supplier_name_snapshot:string;currency_code:string;total_amount:string;line_count:number;has_outstanding:boolean;quantity_totals:Totals[];lines:Line[]}
type Result={page:number;page_size:number;total_orders:number;total_lines:number;has_next:boolean;quantity_totals:Totals[];currency_totals:{currency_id:string;currency_code:string;currency_name:string;total_amount:string;order_count:number}[];orders:Order[]}
type Filters={q:string;supplier_id:string;item_id:string;date_from:string;date_to:string;outstanding_only:boolean}
const blank=():Filters=>({q:'',supplier_id:'',item_id:'',date_from:'',date_to:'',outstanding_only:false})
function parameters(filters:Filters,page:number){const p=new URLSearchParams({page:String(page),page_size:'20'});for(const [key,value] of Object.entries(filters))if(value)p.set(key,String(value));return p.toString()}

export function PurchaseOutstanding({onConnection}:{onConnection:(online:boolean)=>void}){
  const [filters,setFilters]=useState<Filters>(blank),[applied,setApplied]=useState<Filters>(blank)
  const [result,setResult]=useState<Result|null>(null),[suppliers,setSuppliers]=useState<Ref[]>([]),[materials,setMaterials]=useState<Ref[]>([])
  const [busy,setBusy]=useState(false),[error,setError]=useState(''),[open,setOpen]=useState<string|null>(null)
  async function load(f=applied,page=result?.page||1){setBusy(true);setError('');try{const data=await api<Result>('/purchase-outstanding?'+parameters(f,page));setResult(data);setApplied({...f});setOpen(null);onConnection(true)}catch(e){setError((e as Error).message);onConnection(false)}finally{setBusy(false)}}
  useEffect(()=>{void load(blank(),1);Promise.all([api<Ref[]>('/suppliers'),api<Ref[]>('/raw-materials')]).then(([ss,ms])=>{setSuppliers(ss);setMaterials(ms)}).catch(e=>setError(e.message))},[])
  function change<K extends keyof Filters>(key:K,value:Filters[K]){setFilters(f=>({...f,[key]:value}))}
  async function search(e:FormEvent){e.preventDefault();await load(filters,1)}
  function clear(){const empty=blank();setFilters(empty);void load(empty,1)}
  const quantityRows=(totals:Totals[],prefix:string)=><div className="inquiry-quantities">{totals.map(t=><dl className="po-line-values" key={t.unit_id} data-testid={prefix+'-unit'}><dt>基本單位</dt><dd>{t.unit_code} · {t.unit_name}</dd><dt>訂購量</dt><dd>{t.ordered_qty_base}</dd><dt>已過帳實收（毛額）</dt><dd>{t.gross_received_qty_base}</dd><dt>未到貨差額</dt><dd className={t.outstanding_qty_base.startsWith('-')?'inquiry-negative':''}>{t.outstanding_qty_base}{t.outstanding_qty_base.startsWith('-')?'（超收差額）':''}</dd><dt>已過帳退貨</dt><dd>{t.posted_return_qty_base}</dd><dt>收貨淨留存</dt><dd title="實收毛額減已過帳退貨，並非整體現存量。">{t.net_retained_qty_base}（非現存量）</dd></dl>)}</div>
  return <>
    <div className="page-heading"><div><div className="eyebrow">PURCHASING / OUTSTANDING INQUIRY</div><h1>未到貨採購查詢</h1><p>依原採購明細與已過帳實收查詢，退貨獨立揭露。</p></div></div>
    <div className="page-meta"><span><span className="teal-dot"/>唯讀查詢</span><span>未到貨 = 訂購 − 實收毛額</span><span className="meta-right">D09 退貨補交／重開規則待核定</span></div>
    <section className="panel" aria-label="採購查詢條件"><div className="panel-title"><h2>查詢條件</h2></div><form onSubmit={search}><div className="form-body po-fields">
      <div className="po-wide"><label htmlFor="oq-search">單號、供應商或原料</label><input id="oq-search" value={filters.q} onChange={e=>change('q',e.target.value)} placeholder="搜尋採購單號、供應商、原料或品名規格"/></div>
      <div><label htmlFor="oq-supplier">供應商</label><select id="oq-supplier" value={filters.supplier_id} onChange={e=>change('supplier_id',e.target.value)}><option value="">全部供應商</option>{suppliers.map(s=><option key={s.id} value={s.id}>{s.code} · {s.name}</option>)}</select></div>
      <div><label htmlFor="oq-item">包含原料（顯示整張全部明細）</label><select id="oq-item" value={filters.item_id} onChange={e=>change('item_id',e.target.value)}><option value="">全部原料</option>{materials.map(m=><option key={m.id} value={m.id}>{m.code} · {m.name}</option>)}</select></div>
      <div><label htmlFor="oq-from">採購日期起</label><input id="oq-from" type="date" value={filters.date_from} onChange={e=>change('date_from',e.target.value)}/></div><div><label htmlFor="oq-to">採購日期迄</label><input id="oq-to" type="date" value={filters.date_to} onChange={e=>change('date_to',e.target.value)}/></div>
      <label className="check-control po-wide"><input id="oq-only" type="checkbox" checked={filters.outstanding_only} onChange={e=>change('outstanding_only',e.target.checked)}/>僅顯示有未到貨明細的採購單</label>
    </div><div className="form-actions" data-feedback-group><ActionFeedback error={error} busy={busy}/><button className="button primary" type="submit" disabled={busy}>查詢</button><button className="button" type="button" disabled={busy} onClick={clear}>清除條件</button><button className="button" type="button" disabled={busy} onClick={()=>void load()}>重新整理</button></div></form></section>
    {busy&&<p role="status">正在讀取採購查詢…</p>}
    {error&&result&&<p className="field-note">查詢未完成，下方保留上次成功的查詢結果。</p>}
    {result&&<>
      <section className="panel inquiry-summary" aria-label="全部符合結果摘要"><div className="panel-title"><h2>符合結果 <span data-testid="oq-order-count">{result.total_orders}</span> 張 / {result.total_lines} 筆明細</h2></div><p className="field-note po-policy">摘要包含全部符合採購單，不限目前頁面；數量依基本單位分組，金額依幣別分組，不混加不同單位或幣別。</p>
        {quantityRows(result.quantity_totals,'oq-summary')}{result.currency_totals.map(c=><p className="po-policy" key={c.currency_id} data-testid="oq-currency-total">{c.currency_code} · 原採購 SUM {c.total_amount}（{c.order_count} 張）</p>)}
      </section>
      <div className="inquiry-orders" aria-label="採購查詢結果">{result.orders.map(order=><section className="panel" key={order.id} data-testid={'oq-order-'+order.document_no}><div className="panel-title"><h2>{order.document_no}</h2><span>{order.order_date}</span></div><div className="po-line-heading"><strong>{order.supplier_code} · {order.supplier_name_snapshot}</strong><span>原採購 SUM <b data-testid="oq-order-sum">{order.total_amount}</b> · {order.currency_code}</span></div>
        {quantityRows(order.quantity_totals,'oq-order')}
        <div className="form-actions"><button className="button" type="button" aria-expanded={open===order.id} disabled={busy} onClick={()=>setOpen(open===order.id?null:order.id)}>{open===order.id?'收合明細':'查看全部明細'} · {order.document_no}</button><a className="button" href={'#purchase-orders?record='+encodeURIComponent(order.id)}>查看原採購單 · {order.document_no}</a></div>
        {open===order.id&&<div aria-label={order.document_no+' 採購明細'}>{order.lines.map(line=><article className="po-line" key={line.id} data-testid={'oq-line-'+line.line_no}><div className="po-line-heading"><strong>明細 {line.line_no} · {line.item_code} · {line.item_name}</strong><span>原金額 {line.line_amount}</span></div><dl className="po-line-values"><dt>品名規格</dt><dd>{line.description_snapshot}</dd><dt>原訂量 × 原單價</dt><dd>{line.ordered_qty} × {line.unit_price}</dd><dt>基本單位</dt><dd>{line.unit_code} · {line.unit_name}</dd><dt>預計到貨日</dt><dd>{line.due_date}</dd><dt>預計收貨倉</dt><dd>{line.warehouse_code} · {line.warehouse_name}</dd><dt>訂購 / 實收毛額 / 未到貨</dt><dd data-testid="oq-line-quantities">{line.ordered_qty_base} / {line.gross_received_qty_base} / {line.outstanding_qty_base}</dd><dt>已過帳退貨</dt><dd data-testid="oq-line-returned">{line.posted_return_qty_base}</dd><dt>收貨淨留存（非現存量）</dt><dd data-testid="oq-line-net">{line.net_retained_qty_base}</dd></dl>
          {line.outstanding_qty_base.startsWith('-')&&<p className="po-policy inquiry-negative">實收大於訂購，保留顯示負差額；本查詢不變更或核准超收規則。</p>}
          {line.receipts.length?<div className="inquiry-sources">{line.receipts.map(r=><p key={r.receipt_line_id}><a href={'#purchase-receipts?record='+encodeURIComponent(r.receipt_id)}>{r.document_no} · 明細 {r.receipt_line_no}</a> · {r.business_date} · 實收 {r.quantity_base} · 已退 {r.posted_return_qty_base}</p>)}</div>:<p className="po-policy">尚無已過帳的收貨明細。</p>}
        </article>)}</div>}
      </section>)}</div>
      {!result.orders.length&&!busy&&<div className="empty-state"><h2>{result.total_orders?'此頁沒有資料':'沒有符合條件的採購單'}</h2><p>可調整日期、供應商或原料條件，或清除條件重新查詢。</p></div>}
      <div className="form-actions inquiry-pagination"><button className="button" type="button" disabled={busy||result.page<=1} onClick={()=>void load(applied,result.page-1)}>上一頁</button><span>第 {result.page} 頁 · 每頁 {result.page_size} 張</span><button className="button" type="button" disabled={busy||!result.has_next} onClick={()=>void load(applied,result.page+1)}>下一頁</button></div>
    </>}
    <footer className="page-footer">TIGER ERP <span>未到貨採購唯讀查詢</span><span>退貨是否重開未到貨：D09 待核定</span></footer>
  </>
}
