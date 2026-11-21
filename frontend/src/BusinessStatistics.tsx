import {Fragment, useEffect, useRef, useState, type FormEvent} from 'react'
import {api} from './api'
import {ActionFeedback} from './ActionFeedback'
import './BusinessStatistics.css'

type Area='purchasing'|'manufacturing'|'sales'
type Filters={date_from:string;date_to:string;party_id:string;item_id:string;document_no:string;work_order_id:string}
type Named={id:string;code:string;name:string}
type Options={read_only:true;area:Area;parties:Named[];items:(Named&{item_kind:string})[];work_orders:{id:string;document_no:string}[]}
type Count={key:string;label:string;count:number}
type Amount={currency_id:string|null;currency_code:string|null;currency_name:string|null;amount:string;order_count:number;label:string;order_id?:string;document_no?:string}
type Quantities={unit_id:string;unit_code:string;unit_name:string;[key:string]:string}
type Group={id:string;code:string;name:string;counts:Count[];amounts:Amount[];quantities:Quantities[]}
type RelatedDocument={kind:string;id:string;document_no:string}
type Detail={id:string;source_id:string;source_kind:string;document_no:string;event_date:string|null;date_basis:string;business_date:string|null;state:string|null;
 party_id:string|null;party_code:string|null;party_name:string|null;item_id:string|null;item_code:string|null;item_name:string|null;item_kind:string|null;
 unit_id:string|null;unit_code:string|null;unit_name:string|null;quantity_base:string|null;amount:string|null;currency_id:string|null;currency_code:string|null;currency_name:string|null;
 order_id:string|null;order_no:string|null;order_line_id:string|null;source_line_id:string|null;work_order_id:string|null;work_order_no:string|null;
 related_documents:RelatedDocument[];metrics:Record<string,string>;line_no?:number;unit_price?:string;ordered_qty?:string;base_factor_snapshot?:string;good_qty_base?:string;
 defective_qty_base?:string;planned_finish?:string;restock_eligible?:boolean;posting_id?:string;warehouse_id?:string;warehouse_code?:string;lot_id?:string;lot_code?:string;qty_base_delta?:string}
type Source={key:string;label:string;total:number;offset:number;limit:number;rows:Detail[]}
type Indicator={key:string;label:string;unit_id:string;unit_code:string;numerator:string;denominator:string;expression:string;note:string}
type Report={read_only:true;area:Area;as_of:string;scope:Record<string,unknown>;notes:string[];summary:{counts:Count[];amounts:Amount[];quantities:Quantities[];indicators?:Indicator[]};breakdowns:{key:string;label:string;rows:Group[]}[];details:Source[]}

const areas:{key:Area;label:string;description:string}[]=[
 {key:'purchasing',label:'採購統計',description:'採購訂單原值、已過帳收貨與供應商退貨，分別核對原始來源。'},
 {key:'manufacturing',label:'製造統計',description:'工單計畫、已確認完工與實際入出庫分別統計，保留各自單位。'},
 {key:'sales',label:'銷售統計',description:'銷售訂單原值、已過帳出貨與已完成客戶退貨，分別揭露實體退庫。'},
]
const metricLabels:Record<string,string>={ordered_qty_base:'訂購量',gross_received_qty_base:'已過帳實收毛額',posted_return_qty_base:'已過帳供應商退貨',net_retained_qty_base:'收貨淨留存',current_outstanding_qty_base:'目前未到貨',gross_shipped_qty_base:'已過帳出貨毛額',finalized_returned_qty_base:'已完成客戶退貨',restocked_return_qty_base:'已實體退庫',nonrestocked_return_qty_base:'已登記未退庫',net_physical_shipped_qty_base:'實體出貨淨留存',planned_qty_base:'工單計畫量',confirmed_good_qty_base:'已確認良品',confirmed_defective_qty_base:'已確認不良品',confirmed_total_qty_base:'已確認回報總量',fg_received_qty_base:'已過帳成品入庫',material_issued_qty_base:'已過帳生產領料',material_returned_qty_base:'已過帳生產退料',net_material_issued_qty_base:'生產淨領料',current_gross_received_qty_base:'目前累計實收毛額',current_gross_shipped_qty_base:'目前累計出貨毛額'}
const areaMetrics:Record<Area,string[]>={purchasing:['ordered_qty_base','gross_received_qty_base','posted_return_qty_base','net_retained_qty_base','current_outstanding_qty_base'],manufacturing:['planned_qty_base','confirmed_good_qty_base','confirmed_defective_qty_base','confirmed_total_qty_base','fg_received_qty_base','material_issued_qty_base','material_returned_qty_base','net_material_issued_qty_base'],sales:['ordered_qty_base','gross_shipped_qty_base','finalized_returned_qty_base','restocked_return_qty_base','nonrestocked_return_qty_base','net_physical_shipped_qty_base','current_outstanding_qty_base']}
const documents:Record<string,string>={PURCHASE_ORDER:'採購訂單',PURCHASE_ORDER_LINE:'採購明細',PURCHASE_RECEIPT:'採購入庫',SUPPLIER_RETURN:'供應商退貨',WORK_ORDER:'製造工單',PRODUCTION_COMPLETION:'完工回報',FINISHED_GOODS_RECEIPT:'成品入庫',MATERIAL_ISSUE:'生產領料',MATERIAL_RETURN:'生產退料',SALES_ORDER:'銷售訂單',SALES_ORDER_LINE:'銷售明細',SALES_SHIPMENT:'銷售出貨',CUSTOMER_RETURN:'客戶退貨',CUSTOMER_RETURN_NO_RESTOCK:'客戶退貨登記（未退庫）'}
const dateLabels:Record<string,string>={ORDER_DATE:'訂單業務日',PLANNED_START:'預計開工日',CONFIRMED_AT_UTC:'完工確認時間（UTC）',POSTED_AT_UTC:'實際過帳時間（UTC）',FINALIZED_AT_UTC:'客退完成時間（UTC）'}
const dateNotes:Record<Area,string>={purchasing:'日期篩選依據：採購訂單使用訂單業務日；實收與供應商退貨使用各自的實際過帳時間（UTC）。',manufacturing:'日期篩選依據：工單計畫使用預計開工日；已確認完工使用完工確認時間；成品入庫、領料與退料使用各自實際過帳時間（時間戳記皆為 UTC）。',sales:'日期篩選依據：銷售訂單使用訂單業務日；出貨使用實際過帳時間；已完成客退使用客退完成時間（時間戳記皆為 UTC）。'}
const stateLabels:Record<string,string>={DRAFT:'草稿',CONFIRMED:'已確認',POSTED:'已過帳',RECORDED_NO_RESTOCK:'已完成登記／未退庫',CANCELLED:'已取消',RELEASED:'已開立',CLOSED:'已結案'}
const filterKeys=['date_from','date_to','party_id','item_id','document_no','work_order_id'] as const
const pageSize=50
const blank=():Filters=>({date_from:'',date_to:'',party_id:'',item_id:'',document_no:'',work_order_id:''})
function selection(){
 const p=new URLSearchParams(window.location.hash.split('?')[1]||'')
 const area:Area=p.get('area')==='manufacturing'?'manufacturing':p.get('area')==='sales'?'sales':'purchasing'
 const filters=blank();for(const key of filterKeys)filters[key]=p.get(key)||''
 const raw=p.get('offset')||'0',offset=/^\d+$/.test(raw)&&Number.isSafeInteger(Number(raw))?Number(raw):0
 return {area,filters,offset}
}
function query(area:Area,filters:Filters,offset:number){
 const p=new URLSearchParams({area,offset:String(offset),limit:String(pageSize)})
 for(const key of filterKeys)if(filters[key])p.set(key,filters[key])
 return p
}
function remember(area:Area,filters:Filters,offset:number){
 const p=query(area,filters,offset);p.delete('limit');if(!offset)p.delete('offset')
 window.history.replaceState(null,'','#business-statistics?'+p.toString())
}
function metricLabel(key:string,area:Area){return key==='current_outstanding_qty_base'&&area==='sales'?'目前未出貨':metricLabels[key]||key}
function amountKey(value:Amount,index:number){return value.order_id||value.currency_id||value.document_no||String(index)}
function AmountValue({value}:{value:Amount}){
 return <div className="statistics-amount"><strong className="statistics-number" data-testid="statistics-amount-value">{value.amount}</strong><span>{value.currency_code?value.currency_code+' · '+(value.currency_name||''):value.document_no?value.document_no+' · 此單原值／幣別未記錄':'幣別未記錄'}</span><small>{value.label}{value.currency_code?' · '+value.order_count+' 張訂單':''}</small></div>
}
function QuantityList({rows,area}:{rows:Quantities[];area:Area}){
 return <div className="statistics-unit-groups">{rows.map(unit=><div className="statistics-unit-group" key={unit.unit_id}><strong>{unit.unit_code} · {unit.unit_name}</strong><dl>{Object.entries(unit).filter(([key])=>key!=='unit_id'&&key!=='unit_code'&&key!=='unit_name').map(([key,value])=><Fragment key={key}><dt>{metricLabel(key,area)}</dt><dd className="statistics-number">{value}</dd></Fragment>)}</dl></div>)}</div>
}
function SourceRecord({row,area}:{row:Detail;area:Area}){
 const identities:[string,string|null|undefined][]=[['來源識別',row.source_id],['明細識別',row.source_line_id],['訂單識別',row.order_id],['訂單明細識別',row.order_line_id],['製造工單識別',row.work_order_id],['過帳識別',row.posting_id],['品項識別',row.item_id],['基本單位識別',row.unit_id],['倉庫識別',row.warehouse_id],['全域庫存批號識別',row.lot_id]]
 return <details className="statistics-record"><summary>查看來源識別與原始數值</summary><dl className="statistics-record-values">
  <dt>來源類別</dt><dd>{documents[row.source_kind]||row.source_kind}</dd><dt>紀錄識別</dt><dd>{row.id}</dd>
  {identities.filter(([,value])=>value!==null&&value!==undefined&&value!=='').map(([label,value])=><Fragment key={label}><dt>{label}</dt><dd>{value}</dd></Fragment>)}
  {row.line_no!=null&&<><dt>明細序號</dt><dd>{row.line_no}</dd></>}
  {row.state&&<><dt>原始狀態</dt><dd>{stateLabels[row.state]||row.state} · {row.state}</dd></>}
  {row.ordered_qty!=null&&<><dt>原訂數量</dt><dd>{row.ordered_qty}</dd></>}
  {row.unit_price!=null&&<><dt>原單價</dt><dd>{row.unit_price}</dd></>}
  {row.base_factor_snapshot!=null&&<><dt>原單位換算係數</dt><dd>{row.base_factor_snapshot}</dd></>}
  {row.good_qty_base!=null&&<><dt>確認良品量</dt><dd>{row.good_qty_base}</dd></>}
  {row.defective_qty_base!=null&&<><dt>確認不良品量</dt><dd>{row.defective_qty_base}</dd></>}
  {row.planned_finish&&<><dt>計畫完工日</dt><dd>{row.planned_finish}</dd></>}
  {typeof row.restock_eligible==='boolean'&&<><dt>實體退庫</dt><dd>{row.restock_eligible?'已實體退庫':'已登記未退庫；無實體入庫量'}</dd></>}
  {row.qty_base_delta!=null&&<><dt>實體庫存異動（帶正負號）</dt><dd>{row.qty_base_delta} {row.unit_code}</dd></>}
  {row.warehouse_code&&<><dt>倉庫</dt><dd>{row.warehouse_code}</dd></>}{row.lot_code&&<><dt>真實批號</dt><dd>{row.lot_code}</dd></>}
  {Object.entries(row.metrics).map(([key,value])=><Fragment key={key}><dt>{metricLabel(key,area)}</dt><dd>{value} {row.unit_code}</dd></Fragment>)}
 </dl>{row.related_documents.length>0&&<div className="statistics-related">{row.related_documents.map((doc,index)=><p key={doc.kind+doc.id+index}>{documents[doc.kind]||doc.kind}：<strong>{doc.document_no}</strong><small>{doc.id}</small></p>)}</div>}</details>
}
function SourceTable({source,area}:{source:Source;area:Area}){
 return <div className="table-wrap"><table data-testid={'statistics-details-'+source.key}><thead><tr><th scope="col">原始單據 / 日期依據</th><th scope="col">往來對象 / 品項</th><th scope="col">數量 / 訂單原值</th><th scope="col">關聯單據 / 來源明細</th></tr></thead><tbody>{source.rows.map(row=><tr key={row.id} data-testid="statistics-source-row" data-source-id={row.source_id}>
  <td><strong>{row.document_no}</strong>{row.line_no!=null&&<small>明細 {row.line_no}</small>}<small>{dateLabels[row.date_basis]||row.date_basis}：{row.event_date||'未記錄'}</small>{row.business_date&&<small>單據日期：{row.business_date}</small>}{row.state&&<small>{stateLabels[row.state]||row.state}</small>}</td>
  <td>{row.party_code&&<div>{row.party_code} · {row.party_name}</div>}{row.item_code?<div><strong>{row.item_code}</strong><br/>{row.item_name}</div>:<span className="muted">{row.party_code?'整張訂單':'—'}</span>}</td>
  <td>{row.quantity_base!==null&&<div><strong className="statistics-number" data-testid="statistics-source-quantity">{row.quantity_base}</strong> {row.unit_code}<small>{row.unit_name}</small></div>}{row.amount!==null&&<div>原值 <strong className="statistics-number">{row.amount}</strong><small>{row.currency_code||'幣別未記錄；不跨單合計'}</small></div>}{row.quantity_base===null&&row.amount===null&&'—'}{row.restock_eligible===false&&<small>未退庫；無實體入庫</small>}</td>
  <td>{row.order_no&&<div>原訂單：{row.order_no}</div>}{row.work_order_no&&<div>工單：{row.work_order_no}</div>}<SourceRecord row={row} area={area}/></td>
 </tr>)}</tbody></table></div>
}

export function BusinessStatistics({onConnection}:{onConnection:(online:boolean)=>void}){
 const first=useRef(selection()),generation=useRef(0)
 const [area,setArea]=useState<Area>(first.current.area),[filters,setFilters]=useState<Filters>(first.current.filters),[applied,setApplied]=useState<Filters>(first.current.filters)
 const [report,setReport]=useState<Report|null>(null),[options,setOptions]=useState<Options|null>(null)
 const [busy,setBusy]=useState(false),[error,setError]=useState(''),[errorTarget,setErrorTarget]=useState('filters'),[optionsError,setOptionsError]=useState(''),[offset,setOffset]=useState(first.current.offset)
 const [open,setOpen]=useState<Record<string,boolean>>({})
 async function load(nextArea:Area=area,nextFilters:Filters=applied,nextOffset:number=offset,fetchOptions=false,feedbackTarget='filters'){
  const request=++generation.current;setBusy(true);setError('');setErrorTarget(feedbackTarget)
  const optionsRequest=fetchOptions?api<Options>('/business-statistics?'+new URLSearchParams({area:nextArea,mode:'options'}),{cache:'no-store'}):null
  const optionsReady=optionsRequest?optionsRequest.then(value=>{if(request===generation.current){setOptions(value);setOptionsError('')}}).catch((e:Error)=>{if(request===generation.current)setOptionsError('篩選選項讀取失敗：'+e.message)}):Promise.resolve()
  try{
   const value=await api<Report>('/business-statistics?'+query(nextArea,nextFilters,nextOffset),{cache:'no-store'})
   if(request!==generation.current)return
   setReport(value);setApplied({...nextFilters});setOffset(nextOffset);remember(nextArea,nextFilters,nextOffset);onConnection(true)
  }catch(e){if(request===generation.current){setError((e as Error).message);onConnection(false)}}finally{await optionsReady;if(request===generation.current)setBusy(false)}
 }
 useEffect(()=>{
  void load(first.current.area,first.current.filters,first.current.offset,true)
  const restore=()=>{if(window.location.hash.split('?')[0]!=='#business-statistics')return;const next=selection();setArea(next.area);setFilters(next.filters);setReport(null);setOptions(null);setOpen({});void load(next.area,next.filters,next.offset,true)}
  window.addEventListener('hashchange',restore)
  return ()=>{generation.current++;window.removeEventListener('hashchange',restore)}
 },[])
 function change<K extends keyof Filters>(key:K,value:Filters[K]){setFilters(f=>({...f,[key]:value}))}
 function switchArea(next:Area){if(next===area)return;const empty=blank();setArea(next);setFilters(empty);setApplied(empty);setOptions(null);setOptionsError('');setReport(null);setOffset(0);setOpen({});void load(next,empty,0,true)}
 function search(e:FormEvent){e.preventDefault();setOpen({});void load(area,filters,0)}
 function clear(){const empty=blank();setFilters(empty);setOpen({});void load(area,empty,0)}
 function paginate(target:string,nextOffset:number){void load(area,applied,nextOffset,false,target)}
 const definition=areas.find(value=>value.key===area)!
 const activeOptions=options?.area===area?options:null
 const result=report?.area===area?report:null
 const hasNext=!!result?.details.some(source=>source.offset+source.limit<source.total)
 const nonempty=!!result&&(result.summary.counts.some(value=>value.count>0)||result.details.some(source=>source.total>0))
 return <div className="business-statistics" data-testid="business-statistics">
  <div className="page-heading"><div><div className="eyebrow">REPORTS / BUSINESS STATISTICS</div><h1>採購、製造、銷售統計</h1><p>{definition.description}</p></div></div>
  <div className="page-meta"><span><span className="teal-dot"/>唯讀統計</span><span>數量依基本單位分組 · 原始十進位數值</span></div>
  <div className="statistics-tabs" role="group" aria-label="統計領域">{areas.map(tab=><button type="button" className="button" key={tab.key} aria-pressed={area===tab.key} disabled={busy} data-testid={'statistics-tab-'+tab.key} onClick={()=>switchArea(tab.key)}>{tab.label}</button>)}</div>
  <section className="panel" aria-label={definition.label+'查詢條件'} data-testid="statistics-filters"><div className="panel-title"><h2>{definition.label} · 查詢條件</h2><span className="muted">不填日期即查詢全部歷史</span></div>
   <form onSubmit={search}><fieldset className="form-body statistics-filters" disabled={busy}>
    <div><label htmlFor="statistics-from">查詢起日（UTC）</label><input id="statistics-from" type="date" value={filters.date_from} onChange={e=>change('date_from',e.target.value)}/></div>
    <div><label htmlFor="statistics-to">查詢迄日（UTC）</label><input id="statistics-to" type="date" value={filters.date_to} onChange={e=>change('date_to',e.target.value)}/></div>
    {area!=='manufacturing'&&<div><label htmlFor="statistics-party">{area==='purchasing'?'供應商':'客戶'}</label><select id="statistics-party" value={filters.party_id} onChange={e=>change('party_id',e.target.value)}><option value="">{area==='purchasing'?'全部供應商':'全部客戶'}</option>{activeOptions?.parties.map(party=><option key={party.id} value={party.id}>{party.code} · {party.name}</option>)}</select></div>}
    <div><label htmlFor="statistics-item">{area==='purchasing'?'原料':area==='sales'?'成品':'原料或成品'}</label><select id="statistics-item" value={filters.item_id} onChange={e=>change('item_id',e.target.value)}><option value="">全部{area==='purchasing'?'原料':area==='sales'?'成品':'品項'}</option>{activeOptions?.items.map(item=><option key={item.id} value={item.id}>{item.code} · {item.name}</option>)}</select></div>
    <div><label htmlFor="statistics-document">單據號碼（包含文字）</label><input id="statistics-document" value={filters.document_no} onChange={e=>change('document_no',e.target.value)} placeholder="原單據或其關聯來源單號"/></div>
    {area==='manufacturing'&&<div><label htmlFor="statistics-work-order">製造工單</label><select id="statistics-work-order" value={filters.work_order_id} onChange={e=>change('work_order_id',e.target.value)}><option value="">全部製造工單</option>{activeOptions?.work_orders.map(order=><option key={order.id} value={order.id}>{order.document_no}</option>)}</select></div>}
   </fieldset><div className="form-actions" data-feedback-group><ActionFeedback error={(errorTarget==='filters'?error:'')||optionsError}/><span className="statistics-loading" role="status" aria-live="polite">{busy&&errorTarget==='filters'?'正在讀取已提交的統計與來源…':''}</span><button className="button primary" type="submit" disabled={busy} data-testid="statistics-query">查詢</button><button className="button" type="button" disabled={busy} data-testid="statistics-clear" onClick={clear}>清除條件</button><button className="button" type="button" disabled={busy} data-testid="statistics-refresh" onClick={()=>void load(area,applied,offset,true)}>重新整理</button></div></form>
  </section>
  <p className="statistics-policy">{dateNotes[area]} 起日與迄日均包含當日全天；各指標依自己的日期分別篩選。</p>
  {error&&result&&<p className="statistics-stale" role="status">查詢失敗；以下保留上次成功結果，尚未套用新的查詢條件。</p>}
  {result&&<div aria-busy={busy}>
   <section className="statistics-scope" aria-label="統計日期與範圍" data-testid="statistics-scope"><p><strong>結果範圍：</strong>{applied.date_from||'最早紀錄'} 至 {applied.date_to||'全部歷史'}；摘要涵蓋全部符合資料，不限目前明細頁。</p>{result.notes.map((note,index)=><p key={index}>{note}</p>)}<p>讀取時間：<time dateTime={result.as_of}>{result.as_of}</time></p></section>
   <section aria-label={definition.label+'完整摘要'} data-testid="statistics-summary"><div className="statistics-section-title"><h2>{definition.label} · 完整摘要</h2><span>不同單位分開列示</span></div>
    <div className="statistics-counts">{result.summary.counts.map(value=><article className="statistics-count" key={value.key} data-testid={'statistics-metric-'+value.key}><h3>{value.label}</h3><strong className="statistics-number" data-testid="statistics-value">{value.count}</strong><span>張</span></article>)}</div>
    {!!result.summary.amounts.length&&<div className="statistics-amounts" data-testid="statistics-order-amounts">{result.summary.amounts.map((value,index)=><article className="statistics-amount-card" key={amountKey(value,index)}><h3>{area==='purchasing'?'採購訂單 SUM':'銷售訂單原值'}</h3><AmountValue value={value}/></article>)}</div>}
    <div className="statistics-metrics">{areaMetrics[area].map(key=>{const units=result.summary.quantities.filter(unit=>unit[key]!==undefined);return <article className={'statistics-metric '+(key==='current_outstanding_qty_base'?'statistics-current':'')} key={key} data-testid={'statistics-metric-'+key}><h3>{metricLabel(key,area)}</h3>{units.length?units.map(unit=><div className="statistics-metric-value" key={unit.unit_id}><strong className="statistics-number" data-testid="statistics-value">{unit[key]}</strong><span>{unit.unit_code}<small>{unit.unit_name}</small></span></div>):<p>無符合的單位資料</p>}{key==='current_outstanding_qty_base'&&<small className="statistics-metric-note">目前差額，非期末歷史餘額</small>}</article>})}</div>
    {!!result.summary.indicators?.length&&<div className="statistics-indicators">{result.summary.indicators.map(indicator=><article key={indicator.key+indicator.unit_id} data-testid={'statistics-indicator-'+indicator.key}><h3>{indicator.label}</h3><strong className="statistics-number" data-testid="statistics-indicator-expression">{indicator.expression}</strong><span>{indicator.unit_code} / {indicator.unit_code}</span><p>{indicator.note}</p></article>)}</div>}
    {area==='purchasing'&&<p className="statistics-policy">收貨淨留存 = 實收毛額 − 已過帳供應商退貨，並非目前庫存。未到貨沿用原有實收毛額規則；退貨不重開未到貨。訂單 SUM 是訂單原值，非已付費用。</p>}
    {area==='manufacturing'&&<p className="statistics-policy">已確認回報總量 = 良品 + 不良品。完工確認與成品入庫各自呈現，不相加為實體產出。生產淨領料 = 已過帳領料 − 已過帳退料，不代表已核定成本或單位耗用分攤。</p>}
    {area==='sales'&&<p className="statistics-policy">實體出貨淨留存 = 出貨毛額 − 已實體退庫；未退庫登記另列，不產生入庫量。客戶退貨不重開未出貨。銷售訂單原值並非已認列營收、收款或利潤；未記錄幣別的訂單不跨單加總。</p>}
   </section>
   {!nonempty&&!busy&&<p className="statistics-empty" role="status" data-testid="statistics-empty">沒有符合條件的來源資料。可調整篩選條件或清除條件重新查詢。</p>}
   <section className="statistics-breakdowns" aria-label="分組統計" data-testid="statistics-groups"><div className="statistics-section-title"><h2>分組統計</h2></div>{result.breakdowns.map(breakdown=><article className="panel" key={breakdown.key} data-testid={'statistics-group-'+breakdown.key}><div className="panel-title"><h2>{breakdown.label}</h2><span className="muted">{breakdown.rows.length} 組</span></div>{breakdown.rows.length?<div className="table-wrap"><table><thead><tr><th scope="col">分組</th><th scope="col">來源張數</th>{area!=='manufacturing'&&<th scope="col">訂單原值</th>}<th scope="col">數量（基本單位分組）</th></tr></thead><tbody>{breakdown.rows.map(group=><tr key={group.id}><td><strong>{group.code}</strong><br/>{group.name}</td><td>{group.counts.map(count=><p className="statistics-group-count" key={count.key}>{count.label}：<strong>{count.count}</strong></p>)}</td>{area!=='manufacturing'&&<td>{group.amounts.length?group.amounts.map((value,index)=><AmountValue key={amountKey(value,index)} value={value}/>):'—'}</td>}<td><QuantityList rows={group.quantities} area={area}/></td></tr>)}</tbody></table></div>:<p className="statistics-empty">沒有符合的分組資料。</p>}</article>)}</section>
   <section className="statistics-sources" aria-label="可展開的原始來源明細"><div className="statistics-section-title"><h2>原始來源明細</h2><span>各類來源共同頁次 · 摘要涵蓋全部符合結果</span></div>{result.details.map(source=><article className="panel" key={source.key} data-testid={'statistics-source-'+source.key}><div className="panel-title"><h2>{source.label} <span className="count">{source.total} 筆</span></h2><button type="button" className="button" aria-expanded={!!open[source.key]} aria-controls={'statistics-source-body-'+source.key} data-testid={'statistics-expand-'+source.key} onClick={()=>setOpen(value=>({...value,[source.key]:!value[source.key]}))}>{open[source.key]?'收合來源明細':'展開來源明細'}</button></div>{open[source.key]&&<div id={'statistics-source-body-'+source.key}>{source.rows.length?<SourceTable source={source} area={area}/>:<p className="statistics-empty">{source.total?'此類來源在目前頁次沒有明細，可返回第一頁。':'沒有符合條件的來源明細。'}</p>}<div className="form-actions statistics-pagination" data-feedback-group><ActionFeedback error={errorTarget===source.key?error:''}/><span className="statistics-loading" role="status" aria-live="polite">{busy&&errorTarget===source.key?'正在讀取來源明細…':''}</span><button className="button" type="button" disabled={busy||offset===0} data-testid={'statistics-previous-'+source.key} onClick={()=>paginate(source.key,Math.max(0,offset-pageSize))}>上一頁</button><span>本類共 {source.total} 筆；本頁 {source.rows.length} 筆 · 每頁最多 {source.limit} 筆</span><button className="button" type="button" disabled={busy||source.offset+source.limit>=source.total} data-testid={'statistics-next-'+source.key} onClick={()=>paginate(source.key,offset+pageSize)}>下一頁</button>{offset>0&&<button className="button" type="button" disabled={busy} onClick={()=>paginate(source.key,0)}>第一頁</button>}</div></div>}</article>)}
    <div className="form-actions statistics-pagination" data-feedback-group><ActionFeedback error={errorTarget==='pagination'?error:''}/><span className="statistics-loading" role="status" aria-live="polite">{busy&&errorTarget==='pagination'?'正在讀取來源明細…':''}</span><button type="button" className="button" disabled={busy||offset===0} data-testid="statistics-previous" onClick={()=>paginate('pagination',Math.max(0,offset-pageSize))}>來源明細上一頁</button><span>第 {Math.floor(offset/pageSize)+1} 頁 · 各類來源共同頁次</span><button type="button" className="button" disabled={busy||!hasNext} data-testid="statistics-next" onClick={()=>paginate('pagination',offset+pageSize)}>來源明細下一頁</button></div>
   </section>
  </div>}
  <footer className="page-footer">TIGER ERP <span>採購、製造、銷售唯讀統計</span><span>訂單、確認事件與實體過帳分別統計</span></footer>
 </div>
}
