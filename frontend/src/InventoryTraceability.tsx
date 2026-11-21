import {Fragment, useEffect, useRef, useState, type FormEvent, type ReactNode} from 'react'
import {api} from './api'
import {ActionFeedback} from './ActionFeedback'
import './InventoryTraceability.css'

type Connection={onConnection:(online:boolean)=>void}
type View='movements'|'trace'
type ItemKind='ALL'|'RAW'|'FINISHED'
type Evidence='DIRECT'|'WORK_ORDER'|'POOLED'|'UNPROVEN'
type MovementFilters={date_from:string;date_to:string;item_kind:ItemKind;q:string;warehouse_id:string;lot_code:string;movement_type:string;document_no:string}
type Warehouse={id:string;code:string;name:string;mrp_included:boolean;is_active:boolean}
type Movement={id:string;posting_id:string;source_kind:string;source_label:string;source_id:string;source_line_id:string|null;
 document_no:string;posted_at:string;business_date:string;leg_no:number;position_id:string;item_id:string;item_kind:string;
 item_code:string;item_name:string;warehouse_id:string;warehouse_code:string;warehouse_name:string;lot_id:string|null;
 lot_code:string|null;qty_base_delta:string;unit_id:string;unit_code:string;unit_name:string;work_order_id:string|null;
 work_order_no:string|null;party_kind:string|null;party_id:string|null;party_code:string|null;party_name:string|null;
 related_documents:{kind:string;id:string;document_no:string}[]}
type MovementResult={as_of:string;warehouses:Warehouse[];movement_types:{value:string;label:string}[];total:number;offset:number;limit:number;rows:Movement[]}
type Lot={id:string;lot_code:string;item_id:string;item_kind:string;item_code:string;item_name:string;unit_code:string;unit_name:string;origin_kind:string}
type Shipment={id:string;document_no:string;lot_id:string|null;lot_code:string|null;item_code:string;item_name:string;customer_code:string;customer_name:string;quantity:string;unit_code:string}
type TraceOptions={as_of:string;lots:Lot[];shipments:Shipment[];evidence_types:{value:Evidence;label:string}[]}
type TraceFilters={direction:'forward'|'reverse';source_kind:'lot'|'shipment';lot_id:string;shipment_id:string}
type DetailValue=string|number|boolean|null|DetailValue[]|{[key:string]:DetailValue}
type TraceNode={id:string;kind:string;kind_label:string;source_table:string;source_id:string;label:string;item_code:string|null;item_name:string|null;quantity:string|null;unit_code:string|null;details:Record<string,DetailValue>}
type TraceEdge={id:string;from:string;to:string;relationship:string;evidence:Evidence;evidence_label:string;note:string}
type TraceResult={as_of:string;direction:'forward'|'reverse';selection:{lot_id:string|null;shipment_id:string|null};root_node_ids:string[];
 nodes:TraceNode[];edges:TraceEdge[];limitations:{code:string;evidence:Evidence;evidence_label:string;message:string;node_ids:string[]}[];movements:Movement[]}
const movementKeys=['date_from','date_to','item_kind','q','warehouse_id','lot_code','movement_type','document_no'] as const
const pageSize=100
const parameters=()=>new URLSearchParams(window.location.hash.split('?')[1]||'')
const blankMovement=():MovementFilters=>({date_from:'',date_to:'',item_kind:'ALL',q:'',warehouse_id:'',lot_code:'',movement_type:'ALL',document_no:''})
const blankTrace=():TraceFilters=>({direction:'forward',source_kind:'lot',lot_id:'',shipment_id:''})
function initialMovement(){const p=parameters(),f=blankMovement();for(const key of movementKeys)if(key!=='item_kind')f[key]=p.get(key)||f[key];const k=p.get('item_kind');if(k==='RAW'||k==='FINISHED')f.item_kind=k;return f}
function initialOffset(){const raw=parameters().get('offset')||'0';return /^\d+$/.test(raw)&&Number.isSafeInteger(Number(raw))?Number(raw):0}
function initialTrace(){const p=parameters(),f=blankTrace();if(p.get('direction')==='reverse')f.direction='reverse';if(f.direction==='reverse'&&p.get('source_kind')==='shipment')f.source_kind='shipment';f.lot_id=p.get('lot_id')||'';f.shipment_id=p.get('shipment_id')||'';return f}
function remember(view:View,values:Record<string,string>={}){const p=parameters();p.set('view',view);for(const [key,value] of Object.entries(values))if(value)p.set(key,value);else p.delete(key);window.history.replaceState(null,'','#inventory-traceability?'+p.toString())}
function movementQuery(f:MovementFilters,offset:number){const p=new URLSearchParams({offset:String(offset),limit:String(pageSize)});for(const key of movementKeys)if(f[key])p.set(key,f[key]);return p}
const signed=(value:string)=>value!=='0'&&!value.startsWith('-')&&!value.startsWith('+')?'+'+value:value
const dateTime=(value:string)=>new Date(value).toLocaleString('zh-TW',{hour12:false})
const evidenceLabels:Record<Evidence,string>={DIRECT:'已驗證直接關聯',WORK_ORDER:'已驗證工單層級關聯',POOLED:'共同批號來源',UNPROVEN:'無法由現有紀錄精確確認'}
const documentLabels:Record<string,string>={PURCHASE_RECEIPT:'採購入庫',PURCHASE_ORDER:'採購訂單',SUPPLIER_RETURN:'供應商退貨',MATERIAL_ISSUE:'生產領料',MATERIAL_RETURN:'生產退料',WORK_ORDER:'製造工單',PRODUCTION_COMPLETION:'已確認完工',FINISHED_GOODS_RECEIPT:'成品入庫',SALES_SHIPMENT:'銷售出貨',SALES_ORDER:'銷售訂單',CUSTOMER_RETURN:'客戶退貨',STOCKTAKE:'庫存盤點',INVENTORY_ADJUSTMENT:'庫存調整',INVENTORY_TRANSFER:'倉間調撥'}
const detailLabels:Record<string,string>={id:'紀錄識別',document_no:'單據號碼',line_no:'明細序號',item_id:'品項識別',item_code:'品號',item_name:'品名',item_kind:'品項類別',lot_id:'全域庫存批號識別',lot_code:'庫存批號',origin_kind:'批號來源類別',supplier_id:'供應商識別',supplier_code:'供應商代碼',supplier_name:'供應商名稱',supplier_lot_code:'供應商批號',customer_id:'客戶識別',customer_code:'客戶代碼',customer_name:'客戶名稱',warehouse_id:'倉庫識別',warehouse_code:'倉庫代碼',warehouse_name:'倉庫名稱',position_id:'庫存位置識別',work_order_id:'製造工單識別',work_order_no:'製造工單號碼',completion_id:'完工回報識別',completion_no:'完工回報號碼',receipt_id:'入庫單識別',receipt_no:'入庫單號碼',shipment_id:'出貨單識別',shipment_no:'出貨單號碼',issue_id:'領料單識別',issue_no:'領料單號碼',issue_line_id:'原領料明細識別',sales_order_id:'銷售訂單識別',sales_order_line_id:'銷售訂單明細識別',purchase_order_id:'採購訂單識別',purchase_order_line_id:'採購訂單明細識別',stock_document_id:'採購入庫單識別',stock_document_line_id:'採購入庫明細識別',source_position_id:'來源庫存位置識別',destination_position_id:'目的庫存位置識別',source_warehouse_id:'來源倉庫識別',destination_warehouse_id:'目的倉庫識別',quantity:'原紀錄數量',quantity_base:'基本數量',qty_base_delta:'帶正負號庫存異動',qty_base:'基本數量',unit_id:'單位識別',unit_code:'基本單位代碼',unit_name:'基本單位名稱',good_qty_base:'確認良品量',defective_qty_base:'確認不良品量',planned_qty_base:'工單計畫數量',posted_at:'實際過帳時間',confirmed_at:'確認時間',business_date:'單據日期',report_date:'回報日期',state:'單據狀態',posting_state:'過帳狀態',note:'備註',traceability_note:'追溯註記',mrp_included:'納入 MRP',entry_id:'庫存分錄識別',posting_id:'過帳識別',leg_no:'分錄序號',manufactured_date:'製造日期',expiry_date:'有效日期',positions:'庫存位置',warehouses:'倉庫',source_id:'來源紀錄識別',source_line_id:'來源明細識別',source_kind:'來源類別',reason:'原因',quantity_note:'數量說明',relationship:'關聯依據',origin:'原始來源',code:'代碼',name:'名稱'}

function EvidenceBadge({value,label}:{value:Evidence;label?:string}){return <span className={'trace-evidence trace-evidence-'+value.toLowerCase()}>{label||evidenceLabels[value]}</span>}
function detailValue(value:DetailValue):ReactNode{
 if(value===null)return '未記錄'
 if(typeof value==='boolean')return value?'是':'否'
 if(Array.isArray(value))return value.length?<ol>{value.map((v,i)=><li key={i}>{detailValue(v)}</li>)}</ol>:'無'
 if(typeof value==='object')return <RecordDetails values={value}/>
 return String(value)
}
function RecordDetails({values}:{values:Record<string,DetailValue>}){return <dl className="trace-record-values">{Object.entries(values).map(([key,value])=><Fragment key={key}><dt>{detailLabels[key]||key}</dt><dd>{detailValue(value)}</dd></Fragment>)}</dl>}

function MovementTable({rows,testId}:{rows:Movement[];testId:string}){
 return <div className="table-wrap"><table className="trace-movement-table" data-testid={testId}>
  <thead><tr><th scope="col">實際過帳時間</th><th scope="col">異動來源 / 單號</th><th scope="col">品項</th><th scope="col">倉庫</th><th scope="col">真實庫存批號</th><th scope="col">帶正負號異動量</th><th scope="col">工單 / 往來對象</th><th scope="col">識別與關聯單據</th></tr></thead>
  <tbody>{rows.map(row=><tr key={row.id} data-testid="inventory-movement-row" data-entry-id={row.id}>
   <td><time dateTime={row.posted_at} title={row.posted_at}>{dateTime(row.posted_at)}</time><small className="trace-identity">原始時間：{row.posted_at}</small><small className="trace-identity">單據日期：{row.business_date}</small></td>
   <td><strong>{row.source_label}</strong><br/>{row.document_no}{row.source_kind==='INVENTORY_TRANSFER'&&<small className="trace-identity">{row.qty_base_delta.startsWith('-')?'來源倉扣庫':'目的倉入庫'} · 同一調撥單</small>}</td>
   <td>{row.item_kind==='RAW'?'原料':'成品'}<br/><strong>{row.item_code}</strong><br/>{row.item_name}</td>
   <td>{row.warehouse_code}<br/>{row.warehouse_name}</td>
   <td>{row.lot_code||'未分批（依品項設定）'}{row.lot_id&&<small className="trace-identity">全域批號：{row.lot_id}</small>}</td>
   <td><span data-testid="movement-signed-quantity" className={'trace-quantity '+(row.qty_base_delta.startsWith('-')?'negative':'')}>{signed(row.qty_base_delta)}</span><br/>{row.unit_code}<small className="trace-identity">{row.unit_name}</small></td>
   <td>{row.work_order_no&&<div>{row.work_order_no}</div>}{row.party_code&&<div>{row.party_kind==='CUSTOMER'?'客戶':row.party_kind==='SUPPLIER'?'供應商':'往來對象'}：{row.party_code}<br/>{row.party_name}</div>}{!row.work_order_no&&!row.party_code&&'—'}</td>
   <td><details className="trace-movement-details"><summary>查看交易識別與關聯</summary><dl className="trace-record-values"><dt>庫存分錄識別</dt><dd>{row.id}</dd><dt>過帳識別</dt><dd>{row.posting_id}</dd><dt>來源紀錄識別</dt><dd>{row.source_id}</dd><dt>來源明細識別</dt><dd>{row.source_line_id||'未記錄'}</dd><dt>分錄序號</dt><dd>{row.leg_no}</dd><dt>庫存位置識別</dt><dd>{row.position_id}</dd>{row.work_order_id&&<><dt>製造工單識別</dt><dd>{row.work_order_id}</dd></>}{row.party_id&&<><dt>往來對象識別</dt><dd>{row.party_id}</dd></>}</dl><div className="trace-documents">{row.related_documents.map((doc,index)=><p key={doc.kind+doc.id+index}>{documentLabels[doc.kind]||doc.kind}：<strong>{doc.document_no}</strong><small className="trace-identity">{doc.id}</small></p>)}</div></details></td>
  </tr>)}</tbody>
 </table></div>
}

function MovementInquiry({onConnection}:Connection){
 const active=useRef(true)
 const [filters,setFilters]=useState<MovementFilters>(initialMovement),[applied,setApplied]=useState<MovementFilters>(initialMovement)
 const [result,setResult]=useState<MovementResult|null>(null),[busy,setBusy]=useState(false),[error,setError]=useState('')
 async function load(f:MovementFilters=applied,offset:number=result?.offset||0,signal?:AbortSignal){
  setBusy(true);setError('')
  try{const r=await api<MovementResult>('/inventory-movements?'+movementQuery(f,offset),{cache:'no-store',signal});if(signal?.aborted||!active.current)return;setResult(r);setApplied({...f});remember('movements',{...f,offset:String(r.offset)});onConnection(true)}
  catch(e){if(!signal?.aborted&&active.current){setError((e as Error).message);onConnection(false)}}finally{if(!signal?.aborted&&active.current)setBusy(false)}
 }
 useEffect(()=>{active.current=true;const c=new AbortController();void load(initialMovement(),initialOffset(),c.signal);return()=>{active.current=false;c.abort()}},[])
 function change<K extends keyof MovementFilters>(key:K,value:MovementFilters[K]){setFilters(f=>({...f,[key]:value}))}
 function search(e:FormEvent){e.preventDefault();if(filters.date_from&&filters.date_to&&filters.date_from>filters.date_to){setError('過帳日期起不可晚於過帳日期迄。');return}void load(filters,0)}
 function clear(){const f=blankMovement();setFilters(f);void load(f,0)}
 return <>
  <section className="panel" aria-label="庫存異動查詢條件"><div className="panel-title"><h2>庫存異動查詢條件</h2></div>
   <form onSubmit={search}><fieldset disabled={busy} className="form-body po-fields trace-filters">
    <div><label htmlFor="im-date-from">過帳日期起（UTC）</label><input id="im-date-from" type="date" value={filters.date_from} onChange={e=>change('date_from',e.target.value)}/></div>
    <div><label htmlFor="im-date-to">過帳日期迄（UTC）</label><input id="im-date-to" type="date" value={filters.date_to} onChange={e=>change('date_to',e.target.value)}/></div>
    <div><label htmlFor="im-kind">品項類別</label><select id="im-kind" value={filters.item_kind} onChange={e=>change('item_kind',e.target.value as ItemKind)}><option value="ALL">全部原料與成品</option><option value="RAW">原料</option><option value="FINISHED">成品</option></select></div>
    <div><label htmlFor="im-item">品號或品名</label><input id="im-item" value={filters.q} onChange={e=>change('q',e.target.value)}/></div>
    <div><label htmlFor="im-warehouse">倉庫</label><select id="im-warehouse" value={filters.warehouse_id} onChange={e=>change('warehouse_id',e.target.value)}><option value="">全部倉庫</option>{result?.warehouses.map(w=><option key={w.id} value={w.id}>{w.code} · {w.name}{w.is_active?'':'（停用）'}</option>)}</select></div>
    <div><label htmlFor="im-lot">庫存批號（包含文字）</label><input id="im-lot" value={filters.lot_code} onChange={e=>change('lot_code',e.target.value)}/></div>
    <div><label htmlFor="im-type">異動來源類別</label><select id="im-type" value={filters.movement_type} onChange={e=>change('movement_type',e.target.value)}><option value="ALL">全部來源類別</option>{result?.movement_types.filter(t=>t.value!=='ALL').map(t=><option key={t.value} value={t.value}>{t.label}</option>)}</select></div>
    <div><label htmlFor="im-document">來源單號（包含文字）</label><input id="im-document" value={filters.document_no} onChange={e=>change('document_no',e.target.value)}/></div>
   </fieldset><div className="form-actions" data-feedback-group><ActionFeedback error={error}/><span role="status" aria-live="polite" className="trace-loading">{busy?'正在讀取已提交庫存異動…':''}</span><button className="button primary" disabled={busy} type="submit">查詢</button><button className="button" disabled={busy} type="button" onClick={clear}>清除條件</button><button className="button" disabled={busy} type="button" onClick={()=>void load()}>重新整理</button></div></form>
  </section>
  <p className="trace-policy">僅列實際已過帳分錄，保留原始精確數量與基本單位；日期範圍依 UTC 過帳日期含起迄日篩選。調撥的扣庫與入庫分別列示，共用原調撥單及全域批號。未過帳單據與零差異盤點不會形成庫存異動。</p>
  {error&&result&&<p className="trace-failed-result" role="status">本次查詢未完成，下方保留上次成功結果。</p>}
  {result&&<section className="panel trace-result-panel" aria-label="庫存異動查詢結果" aria-busy={busy}>
   <div className="panel-title"><h2>已過帳庫存異動 <span className="count" data-testid="movement-total">{result.total}</span></h2><span className="muted">查詢時間：{dateTime(result.as_of)}</span></div>
   {result.rows.length?<MovementTable rows={result.rows} testId="inventory-movement-table"/>:<p className="empty-state trace-empty">沒有符合條件的已過帳庫存異動。</p>}
   <div className="form-actions trace-pagination"><button className="button" type="button" disabled={busy||result.offset===0} onClick={()=>void load(applied,Math.max(0,result.offset-result.limit))}>上一頁</button><span>{result.rows.length?`第 ${result.offset+1}–${result.offset+result.rows.length} 筆，共 ${result.total} 筆`:`本頁 0 筆，共 ${result.total} 筆`} · 每頁 {result.limit} 筆</span><button className="button" type="button" disabled={busy||result.offset+result.limit>=result.total} onClick={()=>void load(applied,result.offset+result.limit)}>下一頁</button></div>
  </section>}
 </>
}

function EvidenceLegend(){return <div className="trace-evidence-legend"><h3>如何閱讀追溯證據</h3><ul>
 <li><EvidenceBadge value="DIRECT"/>：存在實際保存的紀錄關聯；不代表已記錄每單位耗用分攤。</li>
 <li><EvidenceBadge value="WORK_ORDER"/>：經同一製造工單連接，無法據此認定原料與成品的逐批精確耗用量。</li>
 <li><EvidenceBadge value="POOLED"/>：共用真實批號的來源或去向；多筆收料或生產可能合併，未分配個別數量。</li>
 <li><EvidenceBadge value="UNPROVEN"/>：現有紀錄不足；不補造來源，也不以比例推算耗用。</li>
 </ul></div>}

function TraceResultView({result}:{result:TraceResult}){
 const byId=new Map(result.nodes.map(node=>[node.id,node]))
 const rootLabels=result.root_node_ids.map(id=>byId.get(id)?.label||id)
 function reveal(id:string){const element=document.getElementById('trace-node-'+id);if(element instanceof HTMLDetailsElement){element.open=true;element.focus({preventScroll:true});element.scrollIntoView({block:'center',behavior:'smooth'})}}
 return <section className="panel trace-result-panel" aria-label="製造批號追溯結果" data-testid="trace-result">
  <div className="panel-title"><h2>{result.direction==='forward'?'正向追溯結果':'反向追溯結果'}</h2><span className="muted">查詢時間：{dateTime(result.as_of)}</span></div>
  <p className="trace-section-heading">查詢起點：{rootLabels.join(' ／ ')||'未取得來源紀錄'}</p>
  <p className="trace-direction-note">全域批號識別：{result.selection.lot_id||'依原出貨紀錄回查'}{result.selection.shipment_id&&<> ／ 出貨單識別：{result.selection.shipment_id}</>}</p>
  <EvidenceLegend/>
  {!!result.limitations.length&&<div className="trace-limitations" role="note"><h3>目前證據與精確度限制</h3><ul>{result.limitations.map((limit,index)=><li key={limit.code+index} data-testid="trace-limitation"><EvidenceBadge value={limit.evidence} label={limit.evidence_label}/> {limit.message}</li>)}</ul></div>}
  <h3 className="trace-section-heading">實際關聯路徑 <span className="count">{result.edges.length}</span></h3>
  <p className="trace-direction-note">箭頭固定表示紀錄的來源 → 去向；反向查詢沿相同已保存關聯回查。每一條關聯均個別標示證據類別。點選紀錄名稱可展開下方來源明細。</p>
  {result.edges.length?<ol className="trace-path">{result.edges.map(edge=><li className="trace-edge" key={edge.id} data-testid="trace-edge" data-evidence={edge.evidence}>
   <div className="trace-edge-path"><button className="trace-node-link" type="button" onClick={()=>reveal(edge.from)}>{byId.get(edge.from)?.label||edge.from}</button><span className="trace-arrow" aria-label="來源至去向">→</span><button className="trace-node-link" type="button" onClick={()=>reveal(edge.to)}>{byId.get(edge.to)?.label||edge.to}</button></div>
   <div className="trace-edge-meta"><EvidenceBadge value={edge.evidence} label={edge.evidence_label}/><span>關聯依據：{edge.relationship}</span></div>{edge.note&&<p className="trace-edge-note">{edge.note}</p>}
  </li>)}</ol>:<p className="empty-state trace-empty">無法由現有紀錄精確確認其他來源或去向；下方保留可驗證的紀錄。</p>}
  <h3 className="trace-section-heading">來源紀錄明細 <span className="count">{result.nodes.length}</span></h3>
  <div className="trace-nodes">{result.nodes.map(node=><details className="trace-node" id={'trace-node-'+node.id} key={node.id} data-testid="trace-node" data-source-kind={node.kind} tabIndex={-1}>
   <summary><strong className="trace-record-name">{node.label}</strong><span className="trace-node-kind">{node.kind_label}</span>{result.root_node_ids.includes(node.id)&&<span className="trace-node-selected">查詢起點</span>}{node.quantity!==null&&<span className="trace-quantity">原紀錄數量 {node.quantity} {node.unit_code||''}</span>}</summary>
   <div className="trace-node-body"><dl className="trace-record-values"><dt>來源紀錄識別</dt><dd>{node.source_id}</dd><dt>來源資料表</dt><dd>{node.source_table}</dd>{(node.item_code||node.item_name)&&<><dt>品項</dt><dd>{node.item_code} · {node.item_name}</dd></>}</dl><RecordDetails values={node.details}/></div>
  </details>)}</div>
  <div className="trace-context-heading"><h3>相關批號的實際庫存異動</h3><p>調撥、退貨、調整與盤點依真實分錄列示，作為庫存歷程；它們不會被當成新的製造或採購來源。此處數量是各分錄異動量，並非原料與成品之間的耗用分攤。</p></div>
  {result.movements.length?<MovementTable rows={result.movements} testId="trace-movement-table"/>:<p className="empty-state trace-empty">此追溯範圍沒有可列示的已過帳庫存分錄。</p>}
 </section>
}

function LotTraceability({onConnection}:Connection){
 const active=useRef(true)
 const [filters,setFilters]=useState<TraceFilters>(initialTrace),[result,setResult]=useState<TraceResult|null>(null)
 const [options,setOptions]=useState<TraceOptions|null>(null),[optionsBusy,setOptionsBusy]=useState(false),[traceBusy,setTraceBusy]=useState(false)
 const [error,setError]=useState(''),[optionsError,setOptionsError]=useState('')
 const busy=optionsBusy||traceBusy
 async function loadOptions(signal?:AbortSignal){
  setOptionsBusy(true);setOptionsError('')
  try{const r=await api<TraceOptions>('/lot-traceability?mode=options',{cache:'no-store',signal});if(signal?.aborted||!active.current)return;setOptions(r);onConnection(true)}
  catch(e){if(!signal?.aborted&&active.current){setOptionsError('追溯來源清單讀取失敗：'+(e as Error).message);onConnection(false)}}finally{if(!signal?.aborted&&active.current)setOptionsBusy(false)}
 }
 async function loadTrace(f:TraceFilters,signal?:AbortSignal){
  const selected=f.source_kind==='shipment'?f.shipment_id:f.lot_id
  if(!selected){setError(f.source_kind==='shipment'?'請選擇實際已過帳出貨單。':'請選擇真實庫存批號。');return}
  setTraceBusy(true);setError('')
  const p=new URLSearchParams({direction:f.direction,[f.source_kind==='shipment'?'shipment_id':'lot_id']:selected})
  try{const r=await api<TraceResult>('/lot-traceability?'+p,{cache:'no-store',signal});if(signal?.aborted||!active.current)return;setResult(r);remember('trace',{...f,lot_id:f.source_kind==='lot'?f.lot_id:'',shipment_id:f.source_kind==='shipment'?f.shipment_id:''});onConnection(true)}
  catch(e){if(!signal?.aborted&&active.current){setError((e as Error).message);onConnection(false)}}finally{if(!signal?.aborted&&active.current)setTraceBusy(false)}
 }
 useEffect(()=>{active.current=true;const c=new AbortController(),f=initialTrace();void loadOptions(c.signal);if(f.source_kind==='shipment'?f.shipment_id:f.lot_id)void loadTrace(f,c.signal);return()=>{active.current=false;c.abort()}},[])
 function selectDirection(direction:TraceFilters['direction']){setFilters({direction,source_kind:'lot',lot_id:'',shipment_id:''});setError('')}
 function search(e:FormEvent){e.preventDefault();void loadTrace(filters)}
 function clear(){const f=blankTrace();setFilters(f);setResult(null);setError('');remember('trace',{...f});void loadOptions()}
 function refresh(){void loadOptions();if(filters.source_kind==='shipment'?filters.shipment_id:filters.lot_id)void loadTrace(filters);else setError('')}
 const lots=options?.lots.filter(lot=>lot.item_kind===(filters.direction==='forward'?'RAW':'FINISHED'))||[]
 const selectedLot=options?.lots.find(lot=>lot.id===filters.lot_id)
 const selectedShipment=options?.shipments.find(shipment=>shipment.id===filters.shipment_id)
 return <>
  <section className="panel" aria-label="製造批號追溯條件"><div className="panel-title"><h2>製造批號追溯條件</h2></div>
   <form onSubmit={search} noValidate><fieldset disabled={busy} className="form-body po-fields trace-filters trace-selection-filters">
    <div><label htmlFor="trace-direction">追溯方向</label><select id="trace-direction" data-testid="trace-direction" value={filters.direction} onChange={e=>selectDirection(e.target.value as TraceFilters['direction'])}><option value="forward">正向：原料批號 → 製造與出貨</option><option value="reverse">反向：成品批號或出貨 → 原料來源</option></select></div>
    <div><label htmlFor="trace-source-kind">追溯起點類別</label><select id="trace-source-kind" data-testid="trace-source-kind" value={filters.source_kind} onChange={e=>{setFilters(f=>({...f,source_kind:e.target.value as TraceFilters['source_kind'],lot_id:'',shipment_id:''}));setError('')}}><option value="lot">{filters.direction==='forward'?'原料庫存批號':'成品庫存批號'}</option>{filters.direction==='reverse'&&<option value="shipment">已過帳銷售出貨單</option>}</select></div>
    <div>{filters.source_kind==='lot'?<><label htmlFor="trace-lot-selector">{filters.direction==='forward'?'原料庫存批號':'成品庫存批號'}</label><select id="trace-lot-selector" data-testid="trace-lot-selector" value={filters.lot_id} onChange={e=>setFilters(f=>({...f,lot_id:e.target.value}))}><option value="">請選擇真實庫存批號</option>{lots.map(lot=><option key={lot.id} value={lot.id}>{lot.lot_code} · {lot.item_code} · {lot.item_name}</option>)}</select></>:<><label htmlFor="trace-shipment-selector">已過帳銷售出貨單</label><select id="trace-shipment-selector" data-testid="trace-shipment-selector" value={filters.shipment_id} onChange={e=>setFilters(f=>({...f,shipment_id:e.target.value}))}><option value="">請選擇實際出貨單</option>{options?.shipments.map(shipment=><option key={shipment.id} value={shipment.id}>{shipment.document_no} · {shipment.lot_code||'未分批'} · {shipment.item_code} · {shipment.customer_code}</option>)}</select></>}</div>
    <p className="trace-selection-identity" data-testid="trace-selected-identity">{filters.source_kind==='lot'?(selectedLot?`全域庫存批號識別：${selectedLot.id} ／ 基本單位：${selectedLot.unit_code} · ${selectedLot.unit_name}`:'以全域庫存批號識別查詢，不以倉庫位置或批號文字猜測。'):(selectedShipment?`出貨單識別：${selectedShipment.id} ／ 原出貨量：${selectedShipment.quantity} ${selectedShipment.unit_code} ／ 客戶：${selectedShipment.customer_code} · ${selectedShipment.customer_name}`:'由實際已過帳出貨與原批號關聯回查。')}</p>
   </fieldset><div className="form-actions" data-feedback-group><ActionFeedback error={[optionsError,error].filter(Boolean).join(' ')}/><span className="trace-loading" role="status" aria-live="polite">{busy?'正在讀取真實來源與已記錄關聯…':''}</span><button className="button primary" type="submit" disabled={busy}>開始追溯</button><button className="button" type="button" disabled={busy} onClick={clear}>清除條件</button><button className="button" type="button" disabled={busy} onClick={refresh}>重新整理</button></div></form>
  </section>
  <p className="trace-policy">正向選擇原料全域批號，反向選擇成品全域批號或已過帳出貨。追溯跨倉保留同一批號身分；只呈現現有紀錄能支持的關聯，不推算原料批與成品批之間的逐單位耗用量。</p>
  {error&&result&&<p className="trace-failed-result" role="status">本次追溯未完成，下方保留上次成功結果。</p>}
  {result?<TraceResultView result={result}/>:!busy&&<section className="panel"><p className="empty-state trace-empty">選擇真實批號或出貨單後，按「開始追溯」查看來源與去向。</p><EvidenceLegend/></section>}
 </>
}

export function InventoryTraceability({onConnection}:Connection){
 const [view,setView]=useState<View>(()=>parameters().get('view')==='trace'?'trace':'movements')
 function selectView(next:View){setView(next);remember(next)}
 return <div className="inventory-traceability">
  <div className="page-heading"><div><div className="eyebrow">INQUIRY / INVENTORY MOVEMENTS & LOT TRACEABILITY</div><h1>庫存異動與製造批號雙向追溯</h1><p>查閱實際已過帳庫存分錄，依保存的來源身分追查製造批號與出貨關聯。</p></div></div>
  <div className="page-meta"><span><span className="teal-dot"/>唯讀查詢</span><span>精確基本數量 · 真實全域批號 · 關聯證據分級</span></div>
  <div className="trace-view-switch" role="group" aria-label="查詢視圖"><button className="button" aria-pressed={view==='movements'} type="button" onClick={()=>selectView('movements')}>庫存異動查詢</button><button className="button" aria-pressed={view==='trace'} type="button" onClick={()=>selectView('trace')}>製造批號雙向追溯</button></div>
  {view==='movements'?<MovementInquiry onConnection={onConnection}/>:<LotTraceability onConnection={onConnection}/>}
 </div>
}
