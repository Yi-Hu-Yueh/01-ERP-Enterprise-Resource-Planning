import { ApprovalProvider, ApprovalSelectors } from './ApprovalContext'
import { ApprovalWorkspace, approvalViews, isApprovalView, type ApprovalView } from './ApprovalWorkspace'
import { StandaloneExport, useStandaloneExport } from './StandaloneExport'
import { SQLiteMaintenance, useSQLiteMaintenance } from './SQLiteMaintenance'
import { BusinessStatistics } from './BusinessStatistics'
import { InventoryTraceability } from './InventoryTraceability'
import { InventoryInquiry } from './InventoryInquiry'
import { OutstandingSales } from './OutstandingSales'
import { SalesOrders } from './SalesOrders'
import { SalesShipments } from './SalesShipments'
import { CustomerReturns } from './CustomerReturns'
import { Stocktakes } from './Stocktakes'
import { InventoryAdjustments } from './InventoryAdjustments'
import { InventoryTransfers } from './InventoryTransfers'
import { useEffect, useState, type FormEvent, type ReactNode } from 'react'
import { api } from './api'
import { ActionFeedback } from './ActionFeedback'
import { MasterMaintenance, masterDefinitions, type MasterKind } from './MasterMaintenance'
import { OfferMaintenance } from './OfferMaintenance'
import { PurchaseOrders } from './PurchaseOrders'
import { Receiving } from './Receiving'
import { SupplierReturns } from './SupplierReturns'
import { PurchaseOutstanding } from './PurchaseOutstanding'
import { BomMaintenance } from './BomMaintenance'
import { RoutingMaintenance } from './RoutingMaintenance'
import { WorkOrders } from './WorkOrders'
import { MrpPreview } from './MrpPreview'
import { MaterialIssues } from './MaterialIssues'
import { MaterialReturns } from './MaterialReturns'
import { ProductionCompletions } from './ProductionCompletions'
import { FinishedGoodsReceiving } from './FinishedGoodsReceiving'

type Reference = { id: string; code: string; name: string; row_version: number; is_active: boolean }
type ItemRecord = Reference & { specification: string | null; category_id: string; base_unit_id: string;
  lot_tracking: boolean; created_at: string; updated_at: string; created_by: string; item_kind: string }
type Draft = { code: string; name: string; specification: string; category_id: string; base_unit_id: string;
  lot_tracking: '' | 'true' | 'false'; is_active: boolean }
const empty = (): Draft => ({code:'',name:'',specification:'',category_id:'',base_unit_id:'',lot_tracking:'',is_active:true})
const modules = [
  {name:'基本資料', icon:'◫', operations:['其他基本資料']},
  {name:'採購管理', icon:'↙', operations:[]},
  {name:'製造生產', icon:'⌘', operations:[]},
  {name:'銷售管理', icon:'↗', operations:['銷售訂單','銷售出貨','客戶退貨','銷售未交查詢']},
  {name:'庫存管理', icon:'▦', operations:['庫存盤點','庫存調整','倉間調撥']},
  {name:'查詢與報表', icon:'◷', operations:[]},
  {name:'電子簽核（擴充）', icon:'✓', operations:[]},
]
const toDraft = (m: ItemRecord): Draft => ({code:m.code,name:m.name,specification:m.specification||'',
  category_id:m.category_id,base_unit_id:m.base_unit_id,lot_tracking:m.lot_tracking?'true':'false',is_active:m.is_active})
const time = (value: string) => new Date(value).toLocaleString('zh-TW',{hour12:false})

type ItemKind = 'RAW' | 'FINISHED'
type MasterView=ApprovalView|MasterKind|'business-statistics'|'inventory-traceability'|'inventory-inquiry'|'inventory-transfers'|'inventory-adjustments'|'stocktakes'|'outstanding-sales'|'customer-returns'|'sales-shipments'|'sales-orders'|'finished-goods-receipts'|'production-completions'|'material-returns'|'material-issues'|'mrp-preview'|'work-orders'|'routings'|'boms'|'supplier-offers'|'purchase-orders'|'purchase-receipts'|'supplier-returns'|'purchase-outstanding'
type ViewKind=ItemKind|MasterView
const kindFromHash = (): ViewKind => {const hash=window.location.hash.slice(1).split('?')[0];return isApprovalView(hash)?hash:hash==='finished-goods'?'FINISHED':hash==='business-statistics'||hash==='inventory-traceability'||hash==='inventory-inquiry'||hash==='inventory-transfers'||hash==='inventory-adjustments'||hash==='stocktakes'||hash==='outstanding-sales'||hash==='customer-returns'||hash==='sales-shipments'||hash==='sales-orders'||hash==='finished-goods-receipts'||hash==='production-completions'||hash==='material-returns'||hash==='material-issues'||hash==='mrp-preview'||hash==='work-orders'||hash==='routings'||hash==='boms'||hash==='supplier-offers'||hash==='purchase-orders'||hash==='purchase-receipts'||hash==='supplier-returns'||hash==='purchase-outstanding'?hash:Object.hasOwn(masterDefinitions,hash)?hash as MasterKind:'RAW'}

export default function App(){return <ERPApp/>}

function ERPApp(){
  const standaloneExport=useStandaloneExport()
  const [databaseGeneration,setDatabaseGeneration]=useState(0)
  const sqliteMaintenance=useSQLiteMaintenance(()=>setDatabaseGeneration(value=>value+1))
  const [kind,setKind]=useState<ViewKind>(kindFromHash)
  useEffect(()=>{const sync=()=>setKind(kindFromHash());window.addEventListener('hashchange',sync);return()=>window.removeEventListener('hashchange',sync)},[])
  function selectKind(next: ViewKind){window.location.hash=next==='RAW'?'raw-materials':next==='FINISHED'?'finished-goods':next;setKind(next)}
  return <ApprovalProvider key={databaseGeneration}><ItemWorkspace key={kind+':'+databaseGeneration} kind={kind==='FINISHED'?'FINISHED':'RAW'} master={kind==='RAW'||kind==='FINISHED'?undefined:kind} onKindChange={selectKind} sqliteMaintenance={<SQLiteMaintenance controller={sqliteMaintenance} externalBusy={standaloneExport.running||standaloneExport.submitting||standaloneExport.downloading}/>} standaloneExport={<StandaloneExport controller={standaloneExport} externalBusy={sqliteMaintenance.blocksExport}/>}/></ApprovalProvider>
}

function ItemWorkspace({kind,master,onKindChange,sqliteMaintenance,standaloneExport}:{kind:ItemKind;master?:MasterView;onKindChange:(kind:ViewKind)=>void;sqliteMaintenance:ReactNode;standaloneExport:ReactNode}){
  const noun=kind==='RAW'?'原料':'成品'
  const title=kind==='RAW'?'原料維護':'成品資料維護'
  const endpoint=kind==='RAW'?'/raw-materials':'/finished-goods'

  const [active,setActive]=useState(isApprovalView(master)?6:master==='business-statistics'||master==='inventory-traceability'||master==='inventory-inquiry'?5:master==='stocktakes'||master==='inventory-adjustments'||master==='inventory-transfers'?4:master==='outstanding-sales'||master==='customer-returns'||master==='sales-orders'||master==='sales-shipments'?3:master==='finished-goods-receipts'||master==='production-completions'||master==='material-returns'||master==='material-issues'||master==='mrp-preview'||master==='work-orders'||master==='boms'||master==='routings'||master==='operation-definitions'?2:master==='purchase-orders'||master==='purchase-receipts'||master==='supplier-returns'||master==='purchase-outstanding'?1:0), [showOther,setShowOther]=useState(false)
  const [records,setRecords]=useState<ItemRecord[]>([]),[categories,setCategories]=useState<Reference[]>([]),[units,setUnits]=useState<Reference[]>([])
  const [query,setQuery]=useState(''),[appliedQuery,setAppliedQuery]=useState(''),[online,setOnline]=useState(false)
  const [draft,setDraft]=useState<Draft>(empty),[editing,setEditing]=useState<ItemRecord|null>(null)
  const [error,setError]=useState(''),[notice,setNotice]=useState(''),[busy,setBusy]=useState(false)
  const [listError,setListError]=useState(''),[openError,setOpenError]=useState<{id:string;message:string}|null>(null)
  const [helper,setHelper]=useState<'categories'|'units'|null>(null),[helperCode,setHelperCode]=useState(''),[helperName,setHelperName]=useState(''),[parent,setParent]=useState('')
  const [helperError,setHelperError]=useState(''),[helperBusy,setHelperBusy]=useState(false)
  const [helperErrorTarget,setHelperErrorTarget]=useState('create')
  async function load(q=appliedQuery){
    const [rows,cats,us]=await Promise.all([api<ItemRecord[]>(endpoint+'?q='+encodeURIComponent(q)),api<Reference[]>('/categories'),api<Reference[]>('/units')])
    setRecords(rows);setCategories(cats);setUnits(us);setOnline(true);setListError('')
  }
  useEffect(()=>{if(!master)load('').catch(e=>{setListError(e.message);setOnline(false)})},[])
  function change<K extends keyof Draft>(key: K,value: Draft[K]){setDraft(d=>({...d,[key]:value}))}
  function createNew(){setEditing(null);setDraft(empty());setError('');setNotice('')}
  async function open(id: string){
    try{setError('');setNotice('');setOpenError(null);const row=await api<ItemRecord>(endpoint+'/'+id);setEditing(row);setDraft(toDraft(row))}
    catch(e){setOpenError({id,message:(e as Error).message})}
  }
  async function search(event: FormEvent){
    event.preventDefault();setAppliedQuery(query)
    try{await load(query);setListError('')}catch(e){setListError((e as Error).message);setOnline(false)}
  }
  async function save(event: FormEvent){
    event.preventDefault();setBusy(true);setError('');setNotice('')
    try{
      const body={...draft, specification:draft.specification||null, lot_tracking:draft.lot_tracking==='true',
        ...(editing?{row_version:editing.row_version}:{})}
      const row=await api<ItemRecord>(endpoint+(editing?'/'+editing.id:''),{method:editing?'PUT':'POST',body:JSON.stringify(body)})
      setEditing(row);setDraft(toDraft(row));await load();setNotice(editing?'變更已儲存。':noun+'已新增。')
    }catch(e){setError((e as Error).message)}finally{setBusy(false)}
  }
  async function remove(){
    if(!editing||!window.confirm('確定刪除'+noun+'「'+editing.code+'」？此操作只允許刪除未被引用的資料。'))return
    setBusy(true);setError('')
    try{await api(endpoint+'/'+editing.id+'?row_version='+editing.row_version,{method:'DELETE'});createNew();await load();setNotice(noun+'已刪除。')}
    catch(e){setError((e as Error).message)}finally{setBusy(false)}
  }
  function showHelper(kind: 'categories'|'units'){setHelper(kind);setHelperCode('');setHelperName('');setParent('');setHelperError('')}
  async function addReference(event: FormEvent){
    event.preventDefault();if(!helper)return;setHelperBusy(true);setHelperError('');setHelperErrorTarget('create')
    try{
      const row=await api<Reference>('/'+helper,{method:'POST',body:JSON.stringify({code:helperCode,name:helperName,...(helper==='categories'?{parent_id:parent||null}:{})})})
      change(helper==='categories'?'category_id':'base_unit_id',row.id);await load();setHelper(null);setNotice(helper==='categories'?'分類已新增並選取。':'單位已新增並選取。')
    }catch(e){setHelperError((e as Error).message)}finally{setHelperBusy(false)}
  }
  async function deleteReference(row: Reference){
    if(!helper||!window.confirm('確定刪除「'+row.name+'」？已被引用的資料將受到保護。'))return
    setHelperBusy(true);setHelperError('');setHelperErrorTarget(row.id)
    try{await api('/'+helper+'/'+row.id+'?row_version='+row.row_version,{method:'DELETE'});await load()
      if(draft[helper==='categories'?'category_id':'base_unit_id']===row.id)change(helper==='categories'?'category_id':'base_unit_id','')
    }catch(e){setHelperError((e as Error).message)}finally{setHelperBusy(false)}
  }
  const masterButtons=()=> <><button className={!showOther&&!master&&kind==='RAW'?'sub-selected':''} onClick={()=>{setShowOther(false);onKindChange('RAW')}}>原物料資料維護<span className="ready-dot"/></button><button className={!showOther&&!master&&kind==='FINISHED'?'sub-selected':''} onClick={()=>{setShowOther(false);onKindChange('FINISHED')}}>成品資料維護<span className="ready-dot"/></button>{(Object.keys(masterDefinitions) as MasterKind[]).filter(key=>key!=='operation-definitions').map(key=><button key={key} className={!showOther&&master===key?'sub-selected':''} onClick={()=>{setShowOther(false);onKindChange(key)}}>{masterDefinitions[key].title}<span className="ready-dot"/></button>)}<button className={!showOther&&master==='supplier-offers'?'sub-selected':''} onClick={()=>{setShowOther(false);onKindChange('supplier-offers')}}>供應商供料設定<span className="ready-dot"/></button><button onClick={()=>setShowOther(true)}>其他基本資料<span className="tiny-label">尚未實作</span></button></>
  const referenceName=(rows: Reference[],id: string)=>rows.find(r=>r.id===id)?.name||'—'
  return <div className="app-shell">
    <aside className="sidebar">
      <a className="brand" href="/"><span className="brand-mark">虎</span><span>TIGER<span className="brand-sub">ERP 工作台</span></span></a>
      <div className="workspace-name"><span className="workspace-dot"/>文具製造與批發<span className="local-tag">LOCAL</span></div>
      <p className="nav-caption">工作模組</p>
      <nav aria-label="主要模組">{modules.map((m,i)=><div key={m.name}>
        <button className={'nav-item '+(active===i?'selected':'')} onClick={()=>{setActive(i);setShowOther(false);if(i===0&&(isApprovalView(master)||master==='business-statistics'||master==='inventory-traceability'||master==='inventory-inquiry'||master==='inventory-transfers'||master==='stocktakes'||master==='outstanding-sales'||master==='customer-returns'||master==='sales-shipments'||master==='sales-orders'||master==='finished-goods-receipts'||master==='production-completions'||master==='material-returns'||master==='material-issues'||master==='mrp-preview'||master==='work-orders'||master==='routings'||master==='operation-definitions'||master==='boms'||master==='purchase-orders'||master==='purchase-receipts'||master==='supplier-returns'||master==='purchase-outstanding'))onKindChange('RAW');else if(i===1)onKindChange('purchase-orders');else if(i===2)onKindChange('boms');else if(i===3)onKindChange('sales-orders');else if(i===4)onKindChange('stocktakes');else if(i===5)onKindChange('inventory-inquiry');else if(i===6)onKindChange('approval-inbox')}}><span className="nav-icon">{m.icon}</span>{m.name}<span className="nav-arrow">›</span></button>
        {i===0&&active===0&&<div className="subnav">{masterButtons()}</div>}
        {i===5&&active===5&&<div className="subnav"><button className={master==='inventory-inquiry'&&!showOther?'sub-selected':''} onClick={()=>{setShowOther(false);onKindChange('inventory-inquiry')}}>原料與成品即時庫存查詢<span className="ready-dot"/></button><button className={master==='inventory-traceability'&&!showOther?'sub-selected':''} onClick={()=>{setShowOther(false);onKindChange('inventory-traceability')}}>庫存異動與製造批號雙向追溯<span className="ready-dot"/></button><button className={master==='business-statistics'&&!showOther?'sub-selected':''} onClick={()=>{setShowOther(false);onKindChange('business-statistics')}}>採購、製造、銷售統計<span className="ready-dot"/></button></div>}
        {i===4&&active===4&&<div className="subnav"><button className={master==='stocktakes'&&!showOther?'sub-selected':''} onClick={()=>{setShowOther(false);onKindChange('stocktakes')}}>庫存盤點管理<span className="ready-dot"/></button><button className={master==='inventory-adjustments'&&!showOther?'sub-selected':''} onClick={()=>{setShowOther(false);onKindChange('inventory-adjustments')}}>庫存調整管理<span className="ready-dot"/></button><button className={master==='inventory-transfers'&&!showOther?'sub-selected':''} onClick={()=>{setShowOther(false);onKindChange('inventory-transfers')}}>倉庫調撥管理<span className="ready-dot"/></button><button onClick={()=>setShowOther(true)}>其他庫存作業<span className="tiny-label">尚未實作</span></button></div>}
        {i===3&&active===3&&<div className="subnav"><button className={master==='sales-orders'?'sub-selected':''} onClick={()=>{setShowOther(false);onKindChange('sales-orders')}}>銷售訂單管理<span className="ready-dot"/></button><button className={master==='sales-shipments'?'sub-selected':''} onClick={()=>{setShowOther(false);onKindChange('sales-shipments')}}>銷售出貨管理<span className="ready-dot"/></button><button className={master==='customer-returns'?'sub-selected':''} onClick={()=>{setShowOther(false);onKindChange('customer-returns')}}>客戶退貨管理<span className="ready-dot"/></button><button className={master==='outstanding-sales'?'sub-selected':''} onClick={()=>{setShowOther(false);onKindChange('outstanding-sales')}}>未出貨訂單查詢<span className="ready-dot"/></button></div>}
        {i===1&&active===1&&<div className="subnav"><button className={master==='purchase-orders'?'sub-selected':''} onClick={()=>onKindChange('purchase-orders')}>採購訂單管理<span className="ready-dot"/></button><button className={master==='purchase-receipts'?'sub-selected':''} onClick={()=>onKindChange('purchase-receipts')}>採購入庫管理<span className="ready-dot"/></button><button className={master==='supplier-returns'?'sub-selected':''} onClick={()=>{setShowOther(false);onKindChange('supplier-returns')}}>供應商退貨管理<span className="ready-dot"/></button><button className={master==='purchase-outstanding'?'sub-selected':''} onClick={()=>{setShowOther(false);onKindChange('purchase-outstanding')}}>未到貨採購查詢</button><button onClick={()=>setShowOther(true)}>其他採購作業<span className="tiny-label">尚未實作</span></button></div>}
        {i===2&&active===2&&<div className="subnav"><button className={master==='boms'&&!showOther?'sub-selected':''} onClick={()=>{setShowOther(false);onKindChange('boms')}}>產品 BOM 用料表<span className="ready-dot"/></button><button className={master==='routings'&&!showOther?'sub-selected':''} onClick={()=>{setShowOther(false);onKindChange('routings')}}>製程設定<span className="ready-dot"/></button><button className={master==='operation-definitions'&&!showOther?'sub-selected':''} onClick={()=>{setShowOther(false);onKindChange('operation-definitions')}}>製程作業定義<span className="ready-dot"/></button><button className={master==='work-orders'&&!showOther?'sub-selected':''} onClick={()=>{setShowOther(false);onKindChange('work-orders')}}>製造工單管理<span className="ready-dot"/></button><button className={master==='mrp-preview'&&!showOther?'sub-selected':''} onClick={()=>{setShowOther(false);onKindChange('mrp-preview')}}>智慧物料需求與缺料分析<span className="ready-dot"/></button><button className={master==='material-issues'&&!showOther?'sub-selected':''} onClick={()=>{setShowOther(false);onKindChange('material-issues')}}>生產領料管理<span className="ready-dot"/></button><button className={master==='material-returns'&&!showOther?'sub-selected':''} onClick={()=>{setShowOther(false);onKindChange('material-returns')}}>生產退料管理<span className="ready-dot"/></button><button className={master==='production-completions'&&!showOther?'sub-selected':''} onClick={()=>{setShowOther(false);onKindChange('production-completions')}}>生產完工回報<span className="ready-dot"/></button><button className={master==='finished-goods-receipts'&&!showOther?'sub-selected':''} onClick={()=>{setShowOther(false);onKindChange('finished-goods-receipts')}}>成品入庫管理<span className="ready-dot"/></button></div>}
        {i===6&&active===6&&<div className="subnav approval-post30">{(Object.entries(approvalViews) as [ApprovalView,string][]).map(([key,label])=><button key={key} className={master===key?'sub-selected':''} onClick={()=>{setShowOther(false);onKindChange(key)}}>{label}</button>)}</div>}
      </div>)}</nav>
      {active===6&&<div className="mobile-subnav approval-mobile-nav" aria-label="電子簽核作業">{(Object.entries(approvalViews) as [ApprovalView,string][]).map(([key,label])=><button key={key} className={master===key?'sub-selected':''} onClick={()=>{setShowOther(false);onKindChange(key)}}>{label}</button>)}</div>}
      {active===0&&<div className="mobile-subnav" aria-label="基本資料作業">{masterButtons()}</div>}
      {active===1&&<div className="mobile-subnav" aria-label="採購管理作業"><button className={master==='purchase-orders'?'sub-selected':''} onClick={()=>onKindChange('purchase-orders')}>採購訂單管理</button><button className={master==='purchase-receipts'?'sub-selected':''} onClick={()=>onKindChange('purchase-receipts')}>採購入庫管理</button><button className={master==='supplier-returns'?'sub-selected':''} onClick={()=>{setShowOther(false);onKindChange('supplier-returns')}}>供應商退貨管理</button><button className={master==='purchase-outstanding'?'sub-selected':''} onClick={()=>{setShowOther(false);onKindChange('purchase-outstanding')}}>未到貨採購查詢</button><button onClick={()=>setShowOther(true)}>其他採購作業 · 尚未實作</button></div>}
      {active===2&&<div className="mobile-subnav" aria-label="製造生產作業"><button className={master==='boms'&&!showOther?'sub-selected':''} onClick={()=>{setShowOther(false);onKindChange('boms')}}>產品 BOM 用料表<span className="ready-dot"/></button><button className={master==='routings'&&!showOther?'sub-selected':''} onClick={()=>{setShowOther(false);onKindChange('routings')}}>製程設定<span className="ready-dot"/></button><button className={master==='operation-definitions'&&!showOther?'sub-selected':''} onClick={()=>{setShowOther(false);onKindChange('operation-definitions')}}>製程作業定義<span className="ready-dot"/></button><button className={master==='work-orders'&&!showOther?'sub-selected':''} onClick={()=>{setShowOther(false);onKindChange('work-orders')}}>製造工單管理<span className="ready-dot"/></button><button className={master==='mrp-preview'&&!showOther?'sub-selected':''} onClick={()=>{setShowOther(false);onKindChange('mrp-preview')}}>智慧物料需求與缺料分析<span className="ready-dot"/></button><button className={master==='material-issues'&&!showOther?'sub-selected':''} onClick={()=>{setShowOther(false);onKindChange('material-issues')}}>生產領料管理<span className="ready-dot"/></button><button className={master==='material-returns'&&!showOther?'sub-selected':''} onClick={()=>{setShowOther(false);onKindChange('material-returns')}}>生產退料管理<span className="ready-dot"/></button><button className={master==='production-completions'&&!showOther?'sub-selected':''} onClick={()=>{setShowOther(false);onKindChange('production-completions')}}>生產完工回報<span className="ready-dot"/></button><button className={master==='finished-goods-receipts'&&!showOther?'sub-selected':''} onClick={()=>{setShowOther(false);onKindChange('finished-goods-receipts')}}>成品入庫管理<span className="ready-dot"/></button></div>}
      {active===5&&<div className="mobile-subnav" aria-label="查詢與報表作業"><button className={master==='inventory-inquiry'&&!showOther?'sub-selected':''} onClick={()=>{setShowOther(false);onKindChange('inventory-inquiry')}}>原料與成品即時庫存查詢</button><button className={master==='inventory-traceability'&&!showOther?'sub-selected':''} onClick={()=>{setShowOther(false);onKindChange('inventory-traceability')}}>庫存異動與製造批號雙向追溯</button><button className={master==='business-statistics'&&!showOther?'sub-selected':''} onClick={()=>{setShowOther(false);onKindChange('business-statistics')}}>採購、製造、銷售統計</button></div>}
      {active===4&&<div className="mobile-subnav" aria-label="庫存管理作業"><button className={master==='stocktakes'&&!showOther?'sub-selected':''} onClick={()=>{setShowOther(false);onKindChange('stocktakes')}}>庫存盤點管理</button><button onClick={()=>setShowOther(true)}>其他庫存作業 · 尚未實作</button></div>}
      <div className="sidebar-foot"><span className="round-badge">15</span><div>基礎建置階段<small>主檔資料 · 本機示範</small></div></div>
    </aside>
    <div className="main-shell">
      <header className="topbar"><div className="topbar-context"><span>企業資源規劃 <span className="crumb">／</span> {modules[active].name}</span>{sqliteMaintenance}{standaloneExport}</div><span className={'connection '+(online?'connected':'')}>{online?'● 本機服務已連線':'○ 等待服務連線'}</span></header>
      <div className="demo-banner"><span className="demo-badge">DEMO</span><span>LOCAL DEMO MODE — NO VERIFIED LOGIN</span><span className="demo-note">示範操作員，身分未經驗證</span></div>
      <ApprovalSelectors/>
      <main>
      {isApprovalView(master)?<ApprovalWorkspace view={master} onConnection={setOnline}/>: (active!==0&&!(active===5&&(master==='inventory-inquiry'||master==='inventory-traceability'||master==='business-statistics'))&&!(active===4&&(master==='stocktakes'||master==='inventory-adjustments'||master==='inventory-transfers'))&&!(active===3&&(master==='outstanding-sales'||master==='customer-returns'||master==='sales-orders'||master==='sales-shipments'))&&!(active===2&&(master==='finished-goods-receipts'||master==='production-completions'||master==='material-returns'||master==='material-issues'||master==='mrp-preview'||master==='work-orders'||master==='boms'||master==='routings'||master==='operation-definitions'))&&!(active===1&&(master==='purchase-orders'||master==='purchase-receipts'||master==='supplier-returns'||master==='purchase-outstanding')))||showOther?<section className="planned-page"><div className="eyebrow">規劃中的工作模組</div><h1>{modules[active].name}</h1><p>以下功能尚未實作。</p><div className="planned-list">{(active===3?modules[active].operations.filter(name=>!['銷售訂單','銷售出貨','客戶退貨'].includes(name)):active===4?modules[active].operations.filter(name=>!['庫存盤點','庫存調整','倉間調撥'].includes(name)):modules[active].operations).map(name=><div key={name}><span>{name}</span><span className="planned-tag">尚未實作</span></div>)}</div><button className="button primary" onClick={()=>{setActive(0);setShowOther(false);onKindChange('RAW')}}>返回原料維護</button></section>:master==='business-statistics'?<BusinessStatistics onConnection={setOnline}/>:master==='inventory-traceability'?<InventoryTraceability onConnection={setOnline}/>:master==='inventory-inquiry'?<InventoryInquiry onConnection={setOnline}/>:master==='inventory-transfers'?<InventoryTransfers onConnection={setOnline}/>:master==='inventory-adjustments'?<InventoryAdjustments onConnection={setOnline}/>:master==='stocktakes'?<Stocktakes onConnection={setOnline}/>:master==='outstanding-sales'?<OutstandingSales onConnection={setOnline}/>:master==='customer-returns'?<CustomerReturns onConnection={setOnline}/>:master==='sales-shipments'?<SalesShipments onConnection={setOnline}/>:master==='sales-orders'?<SalesOrders onConnection={setOnline}/>:master==='finished-goods-receipts'?<FinishedGoodsReceiving onConnection={setOnline}/>:master==='production-completions'?<ProductionCompletions onConnection={setOnline}/>:master==='material-returns'?<MaterialReturns onConnection={setOnline}/>:master==='material-issues'?<MaterialIssues onConnection={setOnline}/>:master==='mrp-preview'?<MrpPreview onConnection={setOnline}/>:master==='work-orders'?<WorkOrders onConnection={setOnline}/>:master==='routings'?<RoutingMaintenance onConnection={setOnline}/>:master==='boms'?<BomMaintenance onConnection={setOnline}/>:master==='purchase-outstanding'?<PurchaseOutstanding onConnection={setOnline}/>:master==='supplier-returns'?<SupplierReturns onConnection={setOnline}/>:master==='purchase-receipts'?<Receiving onConnection={setOnline}/>:master==='purchase-orders'?<PurchaseOrders onConnection={setOnline}/>:master==='supplier-offers'?<OfferMaintenance onConnection={setOnline}/>:master?<MasterMaintenance resource={master} onConnection={setOnline}/>:<>
        <div className="page-heading"><div><div className="eyebrow">MASTER DATA / {kind==='RAW'?'RAW MATERIALS':'FINISHED GOODS'}</div><h1>{title}</h1><p>{kind==='RAW'?'集中管理紙張、封面、膠與其他製造用料。':'管理成品基本資料，與原料共用品項識別。'}</p></div><button aria-label={'新增'+noun} className="button primary new-button" onClick={createNew}><span>＋</span>新增{noun}</button></div>
        <div className="page-meta"><span><span className="teal-dot"/>共用品項主檔</span><span>僅維護{noun}基本資料</span><span className="meta-right">庫存異動尚未實作</span></div>
        <div className="content-grid">
          <section className="panel list-panel" aria-label={noun+'清單'}>
            <div className="panel-title"><h2>{noun}清單 <span className="count">{records.length}</span></h2><span className="muted">目前顯示</span></div>
            <div className="search-area" data-feedback-group><form className="search-bar" onSubmit={search}><label className="sr-only" htmlFor="search">搜尋品號或品名</label><span aria-hidden="true">⌕</span><input id="search" value={query} onChange={e=>setQuery(e.target.value)} placeholder="搜尋品號或品名"/><button type="submit">搜尋</button></form><ActionFeedback error={listError}/></div>
            <div className="table-wrap"><table><thead><tr><th>品號 / 品名</th><th>分類</th><th>基本單位</th><th>狀態</th><th><span className="sr-only">操作</span></th></tr></thead><tbody>
              {records.map(row=><tr key={row.id} className={editing?.id===row.id?'current-row':''}><td data-feedback-group><button className="record-link" onClick={()=>open(row.id)} aria-label={'開啟 '+row.code}><span>{row.code}</span><strong>{row.name}</strong></button><ActionFeedback error={openError?.id===row.id?openError.message:''}/></td><td>{referenceName(categories,row.category_id)}</td><td>{referenceName(units,row.base_unit_id)}</td><td><span className={'status '+(row.is_active?'active':'inactive')}>{row.is_active?'啟用':'停用'}</span></td><td><button className="row-open" aria-label={'編輯 '+row.code} onClick={()=>open(row.id)}>↗</button></td></tr>)}
            </tbody></table></div>
            {records.length===0&&<div className="empty-state"><span className="empty-icon">▤</span><h3>{appliedQuery?'沒有符合的'+noun:'建立你的第一筆'+noun}</h3><p>{appliedQuery?'試試其他品號或品名。':'先新增分類與單位，再填寫'+noun+'資料。'}</p></div>}
            <div className="table-footer">資料會保存在此電腦，重新整理後仍可讀取。</div>
          </section>
          <section className="panel editor-panel" aria-label={noun+'資料'}>
            <div className="panel-title"><h2>{(editing?'編輯':'新增')+noun}</h2><span className="form-kind">{kind+(editing?' · 編輯中':' · 新資料')}</span></div>
            <form onSubmit={save}>
              <div className="form-body">
                
                
                <label htmlFor="code">品號 <span className="required">*</span></label><input id="code" required value={draft.code} onChange={e=>change('code',e.target.value)} placeholder={kind==='RAW'?'例如 PAPER-A4':'例如 NOTEBOOK-A5'}/>
                <label htmlFor="name">品名 <span className="required">*</span></label><input id="name" required value={draft.name} onChange={e=>change('name',e.target.value)} placeholder={'輸入'+noun+'名稱'}/>
                <label htmlFor="spec">規格</label><textarea id="spec" rows={3} value={draft.specification} onChange={e=>change('specification',e.target.value)} placeholder="尺寸、材質或其他規格"/>
                <div className="field-label"><label htmlFor="category">分類 <span className="required">*</span></label><button type="button" onClick={()=>showHelper('categories')}>＋ 新增分類</button></div>
                <select id="category" required value={draft.category_id} onChange={e=>change('category_id',e.target.value)}><option value="">請選擇分類</option>{categories.map(r=><option key={r.id} value={r.id}>{r.code} · {r.name}{r.is_active?'':'（停用）'}</option>)}</select>
                <div className="field-label"><label htmlFor="unit">基本單位 <span className="required">*</span></label><button type="button" onClick={()=>showHelper('units')}>＋ 新增單位</button></div>
                <select id="unit" required value={draft.base_unit_id} onChange={e=>change('base_unit_id',e.target.value)}><option value="">請選擇單位</option>{units.map(r=><option key={r.id} value={r.id}>{r.code} · {r.name}{r.is_active?'':'（停用）'}</option>)}</select>
                <div className="form-two"><div><label htmlFor="lot">批次管理 <span className="required">*</span></label><select id="lot" required value={draft.lot_tracking} onChange={e=>change('lot_tracking',e.target.value as Draft['lot_tracking'])}><option value="">請選擇</option><option value="true">是</option><option value="false">否</option></select></div><label className="check-control"><input type="checkbox" checked={draft.is_active} onChange={e=>change('is_active',e.target.checked)}/><span>啟用此{noun}</span></label></div>
                <p className="field-note">批次管理僅保存選項，本階段未建立庫存或批次流程。</p>
                {editing&&<details className="audit"><summary>系統紀錄 · 版次 {editing.row_version}</summary><dl><dt>UUID</dt><dd data-testid="record-uuid">{editing.id}</dd><dt>建立時間</dt><dd>{time(editing.created_at)}</dd><dt>最後更新</dt><dd>{time(editing.updated_at)}</dd><dt>建立者</dt><dd>本機示範操作員（未驗證）</dd></dl></details>}
              </div>
              <div className="form-actions" data-feedback-group>
          <ActionFeedback error={error} notice={notice} onReload={editing?()=>open(editing.id):undefined} busy={busy}/><button disabled={busy} className="button primary" type="submit">{busy?'儲存中…':editing?'儲存變更':'儲存'+noun}</button>{editing&&<button className="button danger" disabled={busy} type="button" onClick={remove}>刪除{noun}</button>}{editing&&<button className="button" disabled={busy} type="button" onClick={createNew}>取消</button>}</div>
            </form>
          </section>
        </div>
        <footer className="page-footer">TIGER ERP <span>本機示範版 · {title}</span><span>其他模組尚未實作</span></footer>
      </>}
      </main>
    </div>
    {helper&&<div className="modal-backdrop" onClick={()=>!helperBusy&&setHelper(null)}><section className="modal" role="dialog" aria-modal="true" aria-labelledby="helper-title" onClick={e=>e.stopPropagation()}><div className="panel-title"><h2 id="helper-title">{helper==='categories'?'分類小工具':'單位小工具'}</h2><button className="close-button" aria-label="關閉小工具" disabled={helperBusy} onClick={()=>setHelper(null)}>×</button></div><div className="modal-body">
      <p className="muted">新增後將自動選入目前的{noun}表單。</p>
      
      <form onSubmit={addReference}><label htmlFor="helper-code">代碼</label><input id="helper-code" autoFocus required value={helperCode} onChange={e=>setHelperCode(e.target.value)}/><label htmlFor="helper-name">名稱</label><input id="helper-name" required value={helperName} onChange={e=>setHelperName(e.target.value)}/>
      {helper==='categories'&&<><label htmlFor="parent">上層分類（選填）</label><select id="parent" value={parent} onChange={e=>setParent(e.target.value)}><option value="">無上層分類</option>{categories.map(r=><option key={r.id} value={r.id}>{r.name}</option>)}</select></>}
      <div className="helper-actions" data-feedback-group><ActionFeedback error={helperErrorTarget==='create'?helperError:''}/><button className="button primary helper-save" disabled={helperBusy} type="submit">新增並選取</button></div></form>
      <div className="helper-list"><h3>現有{helper==='categories'?'分類':'單位'}</h3>{(helper==='categories'?categories:units).length===0?<p className="muted">尚無資料。</p>:(helper==='categories'?categories:units).map(row=><div key={row.id} className="helper-row" data-feedback-group><span><strong>{row.code}</strong> {row.name}</span><button disabled={helperBusy} type="button" aria-label={'刪除 '+row.code} onClick={()=>deleteReference(row)}>刪除</button><ActionFeedback error={helperErrorTarget===row.id?helperError:''}/></div>)}</div>
    </div></section></div>}
  </div>
}

