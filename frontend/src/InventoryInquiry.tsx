import {Fragment, useEffect, useState, type FormEvent} from 'react'
import {api} from './api'
import {ActionFeedback} from './ActionFeedback'
import './InventoryInquiry.css'

type Filters={item_kind:'ALL'|'RAW'|'FINISHED';q:string;warehouse_id:string;lot_code:string}
type Position={id:string;lot_id:string|null;lot_code:string|null;origin_kind:string|null;qty_base:string}
type Warehouse={id:string;code:string;name:string;mrp_included:boolean}
type Group=Warehouse&{warehouse_qty_base:string;matched_qty_base:string;positions:Position[]}
type Item={id:string;code:string;name:string;item_kind:string;unit_code:string;unit_name:string;company_qty_base:string;matched_qty_base:string;warehouses:Group[]}
type Result={as_of:string;scope:{location_filtered:boolean};warehouses:Warehouse[];items:Item[]}
const blank=():Filters=>({item_kind:'ALL',q:'',warehouse_id:'',lot_code:''})
const params=()=>new URLSearchParams(location.hash.split('?')[1]||'')
function initial(){const p=params(),f=blank();const kind=p.get('item_kind');if(kind==='RAW'||kind==='FINISHED')f.item_kind=kind;for(const k of ['q','warehouse_id','lot_code'] as const)f[k]=p.get(k)||'';return f}
function query(f:Filters){const p=new URLSearchParams();for(const [k,v] of Object.entries(f))if(v)p.set(k,v);return p}
function remember(f:Filters,id:string|null){const p=query(f);if(id)p.set('item',id);history.replaceState(null,'','#inventory-inquiry?'+p.toString())}

export function InventoryInquiry({onConnection}:{onConnection:(online:boolean)=>void}){
 const [filters,setFilters]=useState<Filters>(initial),[applied,setApplied]=useState<Filters>(initial)
 const [result,setResult]=useState<Result|null>(null),[busy,setBusy]=useState(false),[error,setError]=useState('')
 const [open,setOpen]=useState<string|null>(()=>params().get('item'))
 async function load(f:Filters=applied,expanded:string|null=open){
  setBusy(true);setError('')
  try{const r=await api<Result>('/inventory-inquiry?'+query(f),{cache:'no-store'});setResult(r);setApplied({...f});remember(f,expanded);onConnection(true)}
  catch(e){setError((e as Error).message);onConnection(false)}finally{setBusy(false)}
 }
 useEffect(()=>{void load(initial())},[])
 function change<K extends keyof Filters>(k:K,v:Filters[K]){setFilters(f=>({...f,[k]:v}))}
 async function search(e:FormEvent){e.preventDefault();await load(filters)}
 function clear(){const f=blank();setFilters(f);setOpen(null);void load(f,null)}
 function expand(id:string){const next=open===id?null:id;setOpen(next);remember(applied,next)}
 const warehouse=result?.warehouses.find(w=>w.id===applied.warehouse_id)
 return <div className="inventory-inquiry">
  <div className="page-heading"><div><div className="eyebrow">INQUIRY / PHYSICAL INVENTORY</div><h1>原料與成品即時庫存查詢</h1><p>依已過帳庫存查詢原料與成品；展開品項即可查看各倉與真實批號。</p></div></div>
  <div className="page-meta"><span><span className="teal-dot"/>唯讀查詢</span><span>實體在手量 · 基本單位</span></div>
  <section className="panel" aria-label="庫存查詢條件"><div className="panel-title"><h2>查詢條件</h2></div>
   <form onSubmit={search}><fieldset disabled={busy} className="inventory-filters form-body po-fields">
    <div><label htmlFor="ii-kind">品項類別</label><select id="ii-kind" value={filters.item_kind} onChange={e=>change('item_kind',e.target.value as Filters['item_kind'])}><option value="ALL">全部原料與成品</option><option value="RAW">原料</option><option value="FINISHED">成品</option></select></div>
    <div><label htmlFor="ii-item">品號或品名</label><input id="ii-item" value={filters.q} onChange={e=>change('q',e.target.value)}/></div>
    <div><label htmlFor="ii-warehouse">倉庫</label><select id="ii-warehouse" value={filters.warehouse_id} onChange={e=>change('warehouse_id',e.target.value)}><option value="">全部倉庫</option>{result?.warehouses.map(w=><option key={w.id} value={w.id}>{w.code} · {w.name}</option>)}</select></div>
    <div><label htmlFor="ii-lot">庫存批號（包含文字）</label><input id="ii-lot" value={filters.lot_code} onChange={e=>change('lot_code',e.target.value)}/></div>
   </fieldset><div className="form-actions" data-feedback-group>
    <ActionFeedback error={error}/><span role="status" aria-live="polite">{busy?'正在讀取目前已過帳庫存…':''}</span>
    <button className="button primary" type="submit" disabled={busy}>查詢</button><button className="button" type="button" disabled={busy} onClick={clear}>清除條件</button><button className="button" type="button" disabled={busy} onClick={()=>void load()}>重新整理</button>
   </div></form>
  </section>
  <p className="field-note po-policy">全公司庫存包含全部倉庫與批號，不受 MRP 納入設定影響，也未扣除保留量。數量依各品項的基本單位顯示，不跨品項加總。每次查詢或重新整理均讀取當下已提交資料。</p>
  {error&&result&&<p role="status">查詢失敗，下方為上次成功結果，請重新整理。</p>}
  {result&&<section className="panel" aria-label="庫存查詢結果" aria-busy={busy}>
   <div className="panel-title"><h2>符合品項 <span className="count">{result.items.length}</span></h2><span className="muted">查詢時間：{new Date(result.as_of).toLocaleString('zh-TW',{hour12:false})}</span></div>
   <p className="inventory-scope">明細範圍：{warehouse?warehouse.code+' · '+warehouse.name:'全部倉庫'} ／ {applied.lot_code?'批號包含「'+applied.lot_code+'」':'全部批號'}。{result.scope.location_filtered?'「篩選庫存」只加總此範圍；「全公司庫存」仍保留全部倉庫、全部批號。':'全公司庫存 = 各倉庫存加總。'}</p>
   {!result.items.length?<p className="empty-state">沒有符合條件的品項或庫存位置。</p>:<div className="table-wrap"><table>
    <thead><tr><th scope="col">類別</th><th scope="col">品號 / 品名</th><th scope="col">基本單位</th><th scope="col">全公司庫存</th>{result.scope.location_filtered&&<th scope="col">篩選庫存</th>}<th scope="col">倉庫與批號</th></tr></thead>
    <tbody>{result.items.map(item=><Fragment key={item.id}>
     <tr data-testid={'inventory-item-'+item.code}><td>{item.item_kind==='RAW'?'原料':'成品'}</td><td><strong>{item.code}</strong><br/>{item.name}</td><td>{item.unit_code} · {item.unit_name}</td><td className="inventory-quantity" data-testid="company-stock">{item.company_qty_base}</td>{result.scope.location_filtered&&<td className="inventory-quantity" data-testid="matched-stock">{item.matched_qty_base}</td>}<td><button type="button" className="button" aria-expanded={open===item.id} aria-controls={'inventory-detail-'+item.id} onClick={()=>expand(item.id)}>{open===item.id?'收合明細':'查看明細'} · {item.code}</button></td></tr>
     {open===item.id&&<tr><td colSpan={result.scope.location_filtered?6:5}><div id={'inventory-detail-'+item.id} className="inventory-details" aria-label={item.code+' 庫存明細'}>
      {!item.warehouses.length&&<p>無已登錄庫存位置，未建立假批號或餘額。</p>}
      {item.warehouses.map(w=><article key={w.id} className="inventory-warehouse"><h3>{w.code} · {w.name}</h3><p>全倉庫存（此品項全部批號）：<strong>{w.warehouse_qty_base}</strong> {item.unit_name}{result.scope.location_filtered&&<> ／ 篩選庫存：<strong>{w.matched_qty_base}</strong> {item.unit_name}</>}{!w.mrp_included&&' · 此倉不納入 MRP，實體庫存仍計入'}</p>
       <table><thead><tr><th scope="col">真實批號 / 批號識別</th><th scope="col">來源</th><th scope="col">實體在手量</th></tr></thead><tbody>{w.positions.map(p=><tr key={p.id}><td>{p.lot_code||'未分批（依品項設定）'}{p.lot_id&&<small className="inventory-identity">{p.lot_id}</small>}<small className="inventory-identity">位置：{p.id}</small></td><td>{p.origin_kind==='MANUFACTURED'?'製造成品批':p.origin_kind==='PURCHASED'?'採購批':p.origin_kind||'未分批位置'}</td><td className="inventory-quantity">{p.qty_base} {item.unit_name}</td></tr>)}</tbody></table>
      </article>)}
     </div></td></tr>}
    </Fragment>)}</tbody>
   </table></div>}
  </section>}
 </div>
}
