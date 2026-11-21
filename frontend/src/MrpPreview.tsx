import {useEffect,useRef,useState} from 'react'
import {api} from './api'
import {ActionFeedback} from './ActionFeedback'
import './MrpPreview.css'

type Identity={id:string;code:string;name:string;row_version:number}
type Material=Identity&{unit_id:string;unit_code:string;unit_name:string}
type Choice={id:string;document_no:string;product_code:string;product_name:string;planned_qty_base:string;product_base_unit_code:string;row_version:number;approval_state:string;bom:{id:string;revision:string;row_version:number}|null}
type Warehouse=Identity&{is_active:boolean;mrp_included:boolean}
type Component={component_id:string;component_version:number;line_no:number;purpose_note:string|null;material:Material;usage_qty_base:string;required_qty_base:string|null;warnings:string[]}
type Order={id:string;document_no:string;row_version:number;approval_state:string;product:Material;planned_qty_base:string;bom:{id:string;revision:string;row_version:number;release_state:string;output_basis_qty:string}|null;routing_id:string|null;routing_version:number|null;complete:boolean;warnings:string[];components:Component[]}
type Position={position_id:string;warehouse_id:string;warehouse_code:string;warehouse_name:string;lot_id:string|null;lot_code:string|null;on_hand_qty_base:string}
type Aggregate={material:Material;calculated_demand_qty_base:string;gross_demand_qty_base:string|null;physical_stock_qty_base:string;planning_supply_qty_base:string|null;shortage_qty_base:string|null;suggested_replenishment_qty_base:string|null;positions:Position[];warehouses:{warehouse_id:string;on_hand_qty_base:string}[];warnings:string[]}
type Preview={calculated_at:string;snapshot_started_at:string;label:string;complete:boolean;warehouses:Warehouse[];orders:Order[];materials:Aggregate[];warnings:string[]}
const display=(quantity:string|null,unit:string)=>quantity===null?'未定':quantity+' '+unit
function initial(key:string){return new URLSearchParams(location.hash.split('?')[1]||'').getAll(key)}
function Warnings({values}:{values:string[]}){return values.length?<ul className="mrp-warnings">{values.map((w,i)=><li key={i}>{w}</li>)}</ul>:null}

export function MrpPreview({onConnection}:{onConnection:(value:boolean)=>void}){
 const [orders,setOrders]=useState<Choice[]>([]),[warehouses,setWarehouses]=useState<Warehouse[]>([])
 const [selected,setSelected]=useState<string[]>(()=>initial('wo')),[scope,setScope]=useState<string[]>(()=>initial('wh'))
 const [query,setQuery]=useState(''),[busy,setBusy]=useState(false),[loaded,setLoaded]=useState(false)
 const [error,setError]=useState(''),[notice,setNotice]=useState(''),[result,setResult]=useState<Preview|null>(null)
 const epoch=useRef(0)
 async function load(){
  setBusy(true);setError('');setNotice('');setResult(null);epoch.current++
  try{const [o,w]=await Promise.all([api<Choice[]>('/work-orders'),api<Warehouse[]>('/warehouses')])
   setOrders(o);setWarehouses(w);setLoaded(true);onConnection(true);setNotice('來源已重新讀取；請核對選取與倉庫後重新試算。')
  }catch(e){setError((e as Error).message);onConnection(false)}finally{setBusy(false)}
 }
 useEffect(()=>{void load();return()=>{epoch.current++}},[])
 useEffect(()=>{
  const params=new URLSearchParams()
  selected.forEach(id=>params.append('wo',id));scope.forEach(id=>params.append('wh',id))
  history.replaceState(null,'','#mrp-preview'+(params.size?'?'+params.toString():''))
 },[selected,scope])
 function change(kind:'orders'|'warehouses',id:string){
  const values=kind==='orders'?selected:scope
  const next=values.includes(id)?values.filter(x=>x!==id):[...values,id]
  if(kind==='orders')setSelected(next);else setScope(next)
  epoch.current++;setResult(null);setError('');setNotice('選取已變更，請重新試算。')
 }
 function clear(){
  epoch.current++;setSelected([]);setScope([]);setQuery('');setResult(null);setError('');setNotice('已取消並清除選取，未修改任何業務資料。')
 }
 async function preview(){
  setError('');setNotice('');setResult(null)
  if(!selected.length||!scope.length){setError('請至少選取一張工單及一個倉庫，再進行唯讀試算。');return}
  if(selected.some(id=>!orders.some(o=>o.id===id))||scope.some(id=>!warehouses.some(w=>w.id===id))){
   setError('選取的工單或倉庫已不存在，請取消清除或重新讀取來源後再選取。');return
  }
  const ticket=++epoch.current;setBusy(true)
  try{
   const included=orders.filter(o=>selected.includes(o.id))
   const response=await api<Preview>('/mrp/preview',{method:'POST',body:JSON.stringify({
    work_order_ids:selected,warehouse_ids:scope,
    expected_work_order_versions:Object.fromEntries(included.map(o=>[o.id,o.row_version])),
    expected_bom_versions:Object.fromEntries(included.filter(o=>o.bom).map(o=>[o.bom!.id,o.bom!.row_version]))})})
   if(ticket!==epoch.current)return
   setResult(response);onConnection(true)
   setNotice(response.complete?'試算完成；此為共同供應分析，未分配、保留或扣庫。':'試算完成，但部分來源或政策未定，請查看對應警示。')
  }catch(e){if(ticket===epoch.current)setError((e as Error).message)}finally{setBusy(false)}
 }
 const visible=orders.filter(o=>(o.document_no+' '+o.product_code+' '+o.product_name).toLocaleLowerCase().includes(query.toLocaleLowerCase()))
 return <div className="mrp-page">
  <div className="page-heading"><div><div className="eyebrow">MANUFACTURING / MRP LITE</div><h1>智慧物料需求與缺料分析</h1><p>明確選取工單與倉庫，合併需求後只計入一次共同庫存。</p></div></div>
  <div className="message mrp-policy"><strong>草稿試算／非正式生產需求</strong><p>來源 BOM 尚未正式核准，試算結果僅供規劃參考。未到採購不列庫存；未建立採購單、保留或生產領料。</p></div>
  <section className="panel" aria-label="試算選取">
   <div className="panel-title"><h2>選取工單與庫存範圍</h2><span>已選 {selected.length} 張工單／{scope.length} 個倉庫</span></div>
   <fieldset disabled={busy} className="mrp-selection"><legend className="sr-only">選取來源</legend>
    <label htmlFor="mrp-search">搜尋工單號或成品</label><input id="mrp-search" value={query} onChange={e=>setQuery(e.target.value)} placeholder="搜尋不會清除已選工單"/>
    <div className="table-wrap"><table><thead><tr><th>選取</th><th>工單／成品</th><th>計畫產出量</th><th>來源與狀態</th></tr></thead><tbody>
     {visible.map(o=><tr key={o.id}><td><input type="checkbox" aria-label={'選取工單 '+o.document_no} checked={selected.includes(o.id)} onChange={()=>change('orders',o.id)}/></td><td>{o.document_no}<br/>{o.product_code} · {o.product_name}</td><td>{o.planned_qty_base} {o.product_base_unit_code}</td><td>{o.approval_state} · 資料版次 {o.row_version}<br/>{o.bom?o.bom.revision+' · 資料版次 '+o.bom.row_version:'缺少 BOM，無法計算'}</td></tr>)}
    </tbody></table></div>
    {loaded&&!visible.length&&<p>沒有符合的工單。</p>}
    <p>目前選取：{orders.filter(o=>selected.includes(o.id)).map(o=>o.document_no).join('、')||'尚未選取'}</p>
    <h3>明確選取倉庫</h3><p>多選會合併顯示共同實體在手量；不代表跨倉可互換、品質合格或已承諾。</p>
    <div className="mrp-warehouse-choices">{warehouses.map(w=><label key={w.id}><input type="checkbox" aria-label={'選取倉庫 '+w.code} checked={scope.includes(w.id)} onChange={()=>change('warehouses',w.id)}/>{w.code} · {w.name} {!w.is_active?'（停用）':''}{!w.mrp_included?'（主檔未勾納入 MRP，本次明確選取）':''}</label>)}</div>
   </fieldset>
   <div className="form-actions" data-feedback-group><ActionFeedback error={error} notice={notice}/><div className="button-row">
    <button type="button" className="button primary" disabled={busy||!loaded} onClick={preview}>{busy?'讀取中…':'重新試算'}</button>
    <button type="button" className="button secondary" disabled={busy} onClick={clear}>取消／清除選取</button>
    <button type="button" className="button secondary" disabled={busy} onClick={load}>重新讀取來源</button>
   </div></div>
  </section>
  {result&&<>
   <section className="panel" aria-label="共同物料需求分析">
    <div className="panel-title"><h2>共同物料需求分析</h2><span>{result.complete?'初步分析完整':'部分結果未定'}</span></div>
    <div className="mrp-details"><p>本次倉庫：{result.warehouses.map(w=>w.code+' · '+w.name).join('、')}</p><p>計算時間：{new Date(result.calculated_at).toLocaleString('zh-TW',{hour12:false})}；同一資料庫讀取快照。此為該次結果，資料異動後請重新讀取來源並試算。</p><Warnings values={result.warnings}/></div>
    <div className="table-wrap"><table><thead><tr><th>原物料／基本單位</th><th>合併總需求</th><th>共同實體庫存</th><th>試算共同供應</th><th>合併缺料量</th><th>試算建議補料量（未建立採購單）</th></tr></thead><tbody>
     {result.materials.map(m=><tr key={m.material.id} data-testid={'mrp-material-'+m.material.code}><td>{m.material.code}<br/>{m.material.name}<br/>{m.material.unit_code} · {m.material.unit_name}<Warnings values={m.warnings}/>{m.gross_demand_qty_base===null&&<p>已算明細小計：{display(m.calculated_demand_qty_base,m.material.unit_code)}（不完整）</p>}</td><td data-testid="gross">{display(m.gross_demand_qty_base,m.material.unit_code)}</td><td data-testid="physical">{display(m.physical_stock_qty_base,m.material.unit_code)}</td><td>{display(m.planning_supply_qty_base,m.material.unit_code)}</td><td data-testid="shortage">{display(m.shortage_qty_base,m.material.unit_code)}</td><td>{display(m.suggested_replenishment_qty_base,m.material.unit_code)}</td></tr>)}
    </tbody></table></div>
    {!result.materials.length&&<p className="mrp-details">尚無可辨識物料；請查看工單來源警示，不能將未定需求視為零。</p>}
   </section>
   <section className="panel" aria-label="逐工單用料來源"><div className="panel-title"><h2>逐工單毛需求與計算來源</h2><span>不對個別工單分配庫存</span></div>
    {result.orders.map(o=><article key={o.id} className="mrp-details" data-testid={'mrp-order-'+o.document_no}>
     <h3>{o.document_no} · {o.product.code} · {o.product.name}</h3><p>{o.planned_qty_base} {o.product.unit_code} · {o.approval_state} · 工單資料版次 {o.row_version}</p>
     <p>{o.bom?o.bom.revision+' · '+o.bom.release_state+' · BOM 資料版次 '+o.bom.row_version+' · 產出基準 '+o.bom.output_basis_qty+' '+o.product.unit_code:'未選定 BOM，需求未定'}</p><Warnings values={o.warnings}/>
     <div className="table-wrap"><table><thead><tr><th>用料行／用途</th><th>原料</th><th>精確公式（成品基本單位 → 原料基本單位）</th><th>毛需求</th></tr></thead><tbody>
      {o.components.map(c=><tr key={c.component_id}><td>{c.line_no} · {c.purpose_note||'未填用途'}<br/>資料版次 {c.component_version}</td><td>{c.material.code} · {c.material.name}</td><td>{o.planned_qty_base} × {c.usage_qty_base} ÷ {o.bom?.output_basis_qty}<Warnings values={c.warnings}/></td><td>{display(c.required_qty_base,c.material.unit_code)}</td></tr>)}
     </tbody></table></div>
     <details><summary>查看來源識別</summary><p>工單：{o.id}<br/>成品：{o.product.id} · 版次 {o.product.row_version}<br/>BOM：{o.bom?.id||'未選取'}<br/>途程：{o.routing_id||'未選取'} · 資料版次 {o.routing_version??'無'}</p>{o.components.map(c=><p key={c.component_id}>用料：{c.component_id}；原料：{c.material.id}；基本單位：{c.material.unit_id}</p>)}</details>
    </article>)}
   </section>
   <section className="panel" aria-label="倉庫批次庫存明細"><div className="panel-title"><h2>倉庫與批次實體庫存</h2><span>已過帳收貨＋已過帳退貨帳本</span></div>
    {result.materials.map(m=><div key={m.material.id} className="mrp-details"><h3>{m.material.code} · {m.material.name}（{m.material.unit_code}）</h3>
     <p>{m.warehouses.map(w=>(result.warehouses.find(s=>s.id===w.warehouse_id)?.code||w.warehouse_id)+'：'+w.on_hand_qty_base+' '+m.material.unit_code).join('；')}</p>
     {m.positions.length?<div className="table-wrap"><table><thead><tr><th>倉庫</th><th>批次</th><th>實體在手量</th></tr></thead><tbody>{m.positions.map(p=><tr key={p.position_id}><td>{p.warehouse_code} · {p.warehouse_name}</td><td>{p.lot_code||'無批次'}</td><td>{p.on_hand_qty_base} {m.material.unit_code}<details><summary>位置識別</summary>{p.position_id}<br/>{p.lot_id}</details></td></tr>)}</tbody></table></div>:<p>所選倉庫無此原料的庫存位置；帳面在手量為 0。</p>}
    </div>)}
   </section>
  </>}
 </div>
}
