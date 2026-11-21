import {test,expect,type Page} from '@playwright/test'
import {spawn,execFileSync,type ChildProcess} from 'node:child_process'
import {mkdirSync,createWriteStream,writeFileSync} from 'node:fs'
import path from 'node:path'
import {fileURLToPath} from 'node:url'
import {randomUUID} from 'node:crypto'
import {expectActionFeedback} from './feedback-assertions'

const root=path.resolve(fileURLToPath(new URL('../../',import.meta.url)))
const artifacts=path.resolve(root,process.env.ERP_TEST_ARTIFACTS||'artifacts/manual-round8')
mkdirSync(artifacts,{recursive:true})
const database=path.join(artifacts,'receiving-'+randomUUID()+'.sqlite')
const apiUrl='http://127.0.0.1:8016',uiUrl='http://127.0.0.1:5179'
const py=path.join(root,'.venv','Scripts','python.exe')
let launcher:ChildProcess
const owned:ChildProcess[]=[]
const evidence:Record<string,any>={database,isolated:true,urls:{apiUrl,uiUrl},generatedScreenshots:0}
let source:any,unit:any,warehouse:any,warehouse2:any,raw:any,nontracked:any
let first:any,second:any,pending:any
let originalOrder:any
const receipts='/api/purchase-receipts'
test.use({baseURL:uiUrl,trace:'retain-on-failure',screenshot:'only-on-failure'})
test.describe.configure({mode:'serial'})

async function ready(url:string){await expect.poll(async()=>{try{return (await fetch(url,{signal:AbortSignal.timeout(1000)})).status}catch{return 0}},{timeout:45_000}).toBe(200)}
async function offline(url:string){await expect.poll(async()=>{try{return (await fetch(url,{signal:AbortSignal.timeout(1000)})).status}catch{return 0}},{timeout:20_000}).toBe(0)}
function processState(){return JSON.parse(execFileSync('powershell.exe',['-NoProfile','-Command',"$p=Get-NetTCPConnection -State Listen | Where-Object LocalPort -in 8016,5179; @($p | Select-Object LocalAddress,LocalPort,OwningProcess) | ConvertTo-Json -Compress"],{windowsHide:true,encoding:'utf8'}))}
async function start(){launcher=spawn(py,['-u','scripts/dev.py','--backend-port','8016','--frontend-port','5179','--database',database],{cwd:root,windowsHide:true,stdio:['ignore','pipe','pipe']});owned.push(launcher);launcher.stdout?.pipe(createWriteStream(path.join(artifacts,'launcher.log'),{flags:'a'}));launcher.stderr?.pipe(createWriteStream(path.join(artifacts,'launcher-error.log'),{flags:'a'}));await ready(apiUrl+'/api/health');await ready(uiUrl);expect(launcher.exitCode).toBeNull()}
async function stop(child:ChildProcess){if(child.exitCode!==null)throw new Error('Owned launcher exited unexpectedly; inspect ownership before cleanup');execFileSync('taskkill.exe',['/PID',String(child.pid),'/T','/F'],{windowsHide:true,stdio:'pipe'});await new Promise<void>(resolve=>{if(child.exitCode!==null)resolve();else child.once('exit',()=>resolve())});await offline(apiUrl+'/api/health');await offline(uiUrl)}
async function json(url:string){const response=await fetch(uiUrl+url);expect(response.status).toBe(200);return response.json()}
function sqlState(){const code="import json,sqlite3,sys;from backend.app.decimal_text import exact_sum;c=sqlite3.connect(sys.argv[1]);c.row_factory=sqlite3.Row;positions=[dict(r) for r in c.execute('SELECT * FROM stock_position ORDER BY id')];[(p.update(on_hand_qty_base=exact_sum(r[0] for r in c.execute(\"SELECT e.qty_base_delta FROM stock_entry e JOIN stock_posting s ON s.id=e.posting_id WHERE s.posting_state='POSTED' AND e.position_id=?\",(p['id'],))))) for p in positions];print(json.dumps({'positions':positions,'entryCount':c.execute('SELECT count(*) FROM stock_entry').fetchone()[0],'postedCount':c.execute(\"SELECT count(*) FROM stock_posting WHERE posting_state='POSTED'\").fetchone()[0],'integrity':c.execute('PRAGMA integrity_check').fetchone()[0],'foreignKeyViolations':c.execute('PRAGMA foreign_key_check').fetchall()}))";return JSON.parse(execFileSync(py,['-c',code,database],{cwd:root,windowsHide:true,encoding:'utf8'}))}
test.beforeAll(async({request})=>{
  for(const url of [apiUrl+'/api/health',uiUrl]){let live=false;try{live=(await fetch(url,{signal:AbortSignal.timeout(800)})).ok}catch{}if(live)throw new Error('QA port occupied; nothing stopped')}
  await start();evidence.initialListeners=processState()
  async function create(resource:string,data:any){const response=await request.post('/api/'+resource,{data});expect(response.status()).toBe(201);return response.json()}
  unit=await create('units',{code:'R8-U',name:'基本單位'});const category=await create('categories',{code:'R8-CAT',name:'入庫測試'});const currency=await create('currencies',{code:'R8-CUR',name:'測試幣別'})
  const supplier=await create('suppliers',{code:'R8-S',name:'測試供應商',currency_id:currency.id,is_active:true})
  warehouse=await create('warehouses',{code:'R8-WA',name:'入庫倉 A',is_active:true,mrp_included:false});warehouse2=await create('warehouses',{code:'R8-WB',name:'入庫倉 B',is_active:true,mrp_included:false})
  raw=await create('raw-materials',{code:'R8-X',name:'追批原料 X',category_id:category.id,base_unit_id:unit.id,lot_tracking:true,is_active:true})
  nontracked=await create('raw-materials',{code:'R8-Y',name:'不追批原料 Y',category_id:category.id,base_unit_id:unit.id,lot_tracking:false,is_active:true})
  source=await create('purchase-orders',{request_key:randomUUID(),document_no:'R8-PO',supplier_id:supplier.id,supplier_name_snapshot:supplier.name,currency_id:currency.id,order_date:'2026-11-19'})
  for(const [item,quantity,price] of [[raw,'10','12.5'],[nontracked,'20','8.75']] as const){const parent=await json('/api/purchase-orders/'+source.id);await create('purchase-orders/'+source.id+'/lines',{request_key:randomUUID(),order_version:parent.row_version,item_id:item.id,description_snapshot:item.name,unit_id:unit.id,ordered_qty:quantity,unit_price:price,due_date:'2026-11-20',preferred_warehouse_id:warehouse.id})}
  originalOrder=await json('/api/purchase-orders/'+source.id);expect(originalOrder.total_amount).toBe('300');source=await json(receipts+'/source-orders/'+source.id)
  evidence.fixtures={unit,warehouse,warehouse2,raw,nontracked,originalOrder}
})
test.afterAll(async()=>{for(const child of [...owned].reverse())if(child.exitCode===null)await stop(child);evidence.qaServicesStopped=true;writeFileSync(path.join(artifacts,'receiving-browser-evidence.json'),JSON.stringify(evidence,null,2)+'\n')})

async function header(page:Page,number:string){await page.getByRole('button',{name:'＋ 新增入庫單',exact:true}).click();await page.locator('#rc-document').fill(number);await page.locator('#rc-date').fill('2026-11-19');expect(await page.locator('#rc-date').getAttribute('type')).toBe('date');await page.locator('#rc-order').selectOption(source.id);await page.getByRole('button',{name:'儲存入庫草稿',exact:true}).click();await expect(page.getByRole('status')).toHaveText('入庫草稿已儲存，尚未異動庫存。');return (await json(receipts)).find((r:any)=>r.document_no===number)}
async function addLine(page:Page,index:number,quantity:string,wh=warehouse.id){await page.getByRole('button',{name:'＋ 新增入庫明細',exact:true}).click();await page.locator('#rc-source-line').selectOption(source.lines[index].id);await page.locator('#rc-quantity').fill(quantity);await page.locator('#rc-warehouse').selectOption(wh);if(index===0)await page.locator('#rc-lot').fill('R8-REAL-LOT');else await expect(page.locator('#rc-lot')).toHaveCount(0);await page.getByRole('button',{name:'儲存入庫明細',exact:true}).click();await expect(page.getByRole('status')).toContainText('入庫明細草稿已儲存')}
async function confirmPost(page:Page){page.once('dialog',d=>d.accept());await page.getByRole('button',{name:'確認過帳入庫',exact:true}).click();await expect(page.getByTestId('rc-state')).toHaveText('已過帳 · 唯讀');await expect(page.getByRole('status')).toHaveText('入庫已成功過帳，庫存已依全部明細更新。')}
async function checkStock(page:Page,x:string,y:string){const stock=await json('/api/stock-positions');expect(stock.find((p:any)=>p.item_id===raw.id).on_hand_qty_base).toBe(x);expect(stock.find((p:any)=>p.item_id===nontracked.id).on_hand_qty_base).toBe(y);expect(stock.find((p:any)=>p.item_id===nontracked.id).lot_id).toBeNull();await expect(page.getByTestId('rc-position').filter({hasText:'現存量 '+x})).toHaveCount(1);expect(await json('/api/purchase-orders/'+source.id)).toEqual(originalOrder);return stock}

test('desktop: draft and cancel, one expanded line, real multi-line posting',async({page})=>{
 await page.goto('/#purchase-receipts');await expect(page.getByRole('heading',{name:'採購入庫管理',exact:true})).toBeVisible();first=await header(page,'R8-RC-001')
 let mutations=0;const listener=(r:any)=>{if(['POST','PUT','DELETE'].includes(r.method()))mutations++};page.on('request',listener)
 await page.getByRole('button',{name:'編輯入庫表頭',exact:true}).click();await page.locator('#rc-note').fill('UNSAVED');await page.getByRole('button',{name:'取消',exact:true}).click();await expect(page.getByTestId('rc-note-value')).toHaveText('—');expect(mutations).toBe(0)
 await page.getByRole('button',{name:'＋ 新增入庫明細',exact:true}).click();await page.locator('#rc-source-line').selectOption(source.lines[0].id);await page.locator('#rc-quantity').fill('77');await page.getByRole('button',{name:'取消',exact:true}).click();expect(mutations).toBe(0);page.off('request',listener)
 await addLine(page,0,'6');await addLine(page,1,'2',warehouse2.id)
 await page.getByRole('button',{name:'編輯入庫明細 1',exact:true}).click();await expect(page.getByRole('button',{name:'編輯入庫明細 2',exact:true})).toBeDisabled();await page.getByRole('button',{name:'取消',exact:true}).click()
 expect(await json('/api/stock-positions')).toEqual([]);await expect(page.getByTestId('rc-onhand-1')).toHaveText('0');await confirmPost(page);await expect(page.getByRole('button',{name:'編輯入庫表頭',exact:true})).toHaveCount(0);await expect(page.getByRole('button',{name:'刪除入庫草稿',exact:true})).toHaveCount(0)
 await expect(page.getByTestId('rc-order-total')).toHaveText('300');evidence.afterFirst=await checkStock(page,'6','2');evidence.draftZeroAndCancelNoMutation=true
})

test('desktop: second partial receipt, request-key retry and genuine F5',async({page,request})=>{
 await page.goto('/#purchase-receipts');second=await header(page,'R8-RC-002');await addLine(page,0,'4');await expect(page.getByTestId('rc-quantities-1')).toHaveText('10 / 6 / 4')
 let postBody:any;page.on('request',r=>{if(r.url().endsWith('/post')&&r.method()==='POST')postBody=r.postDataJSON()})
 await confirmPost(page);const response=await request.post(receipts+'/'+second.id+'/post',{data:postBody});expect(response.status()).toBe(200);const repeated=await response.json();expect(repeated.posting_state).toBe('POSTED');await checkStock(page,'10','2')
 const navigation=page.waitForNavigation({waitUntil:'networkidle'});await page.reload();await navigation;expect(await page.evaluate(()=>performance.getEntriesByType('navigation').map(e=>(e as PerformanceNavigationTiming).type))).toContain('reload')
 await page.getByRole('button',{name:'開啟入庫單 R8-RC-002',exact:true}).click();await expect(page.getByTestId('rc-onhand-1')).toHaveText('10');await expect(page.getByTestId('rc-quantities-1')).toHaveText('10 / 10 / 0');await checkStock(page,'10','2')
 pending=await header(page,'R8-RC-DRAFT');await addLine(page,0,'999');evidence.preRestart={receipts:await json(receipts),stock:await json('/api/stock-positions'),sql:sqlState(),listeners:processState(),launcherPid:launcher.pid};evidence.retryStatus=response.status();evidence.realDocumentReload=true
})

test('genuine full launcher/backend/frontend stop and restart: SQL API UI agree',async({page})=>{
 await stop(launcher);evidence.bothServicesObservedOffline=true;await start();const next=processState();expect(next).toHaveLength(2);expect(next.every((r:any)=>r.LocalAddress==='127.0.0.1')).toBe(true);expect(next.every((r:any)=>!evidence.preRestart.listeners.some((old:any)=>old.OwningProcess===r.OwningProcess))).toBe(true)
 expect(await json(receipts)).toEqual(evidence.preRestart.receipts);expect(await json('/api/stock-positions')).toEqual(evidence.preRestart.stock);expect(sqlState()).toEqual(evidence.preRestart.sql)
 await page.goto('/#purchase-receipts');await page.getByRole('button',{name:'開啟入庫單 R8-RC-002',exact:true}).click();await expect(page.getByTestId('rc-onhand-1')).toHaveText('10');await checkStock(page,'10','2');await page.getByRole('button',{name:'開啟入庫單 R8-RC-DRAFT',exact:true}).click();await expect(page.getByTestId('rc-state')).toHaveText('草稿 · 未異動庫存');await expect(page.getByTestId('rc-onhand-1')).toHaveText('10')
 const sql=sqlState();expect(sql.entryCount).toBe(3);expect(sql.postedCount).toBe(2);expect(sql.integrity).toBe('ok');expect(sql.foreignKeyViolations).toEqual([]);evidence.afterRestart={listeners:next,launcherPid:launcher.pid,sql};evidence.draftsNotPostedOrReplayed=true
})

test('mobile: no fabricated lot, cancel and adjacent error, post, prior-function smoke',async({page})=>{
 await page.setViewportSize({width:390,height:844});await page.goto('/#purchase-receipts');const mobile=await header(page,'R8-RC-MOBILE');await addLine(page,1,'-0.125',warehouse2.id)
 page.once('dialog',d=>d.accept());await page.getByRole('button',{name:'確認過帳入庫',exact:true}).click();await expect(page.getByRole('alert')).toContainText('負接受量');await expectActionFeedback(page,'.po-detail > section[aria-label="入庫表頭"] .form-actions');expect((await json('/api/stock-positions')).find((p:any)=>p.item_id===nontracked.id).on_hand_qty_base).toBe('2')
 await page.getByRole('button',{name:'編輯入庫明細 1',exact:true}).click();await page.locator('#rc-quantity').fill('888');await page.getByRole('button',{name:'取消',exact:true}).click();await expect(page.getByTestId('rc-line-1')).toContainText('-0.125')
 await page.getByRole('button',{name:'編輯入庫明細 1',exact:true}).click();await page.locator('#rc-quantity').fill('0.125');await page.getByRole('button',{name:'儲存入庫明細',exact:true}).click();await expect(page.getByRole('status')).toContainText('入庫明細草稿已儲存');await confirmPost(page);await expect(page.getByTestId('rc-onhand-1')).toHaveText('2.125')
 expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);const stock=await json('/api/stock-positions');expect(stock).toHaveLength(2);expect(stock.find((p:any)=>p.item_id===raw.id).on_hand_qty_base).toBe('10');expect(stock.find((p:any)=>p.item_id===nontracked.id).on_hand_qty_base).toBe('2.125');expect((await json(receipts+'/'+mobile.id)).lines[0].lot_id).toBeNull()
 await page.goto('/#purchase-orders');await page.getByRole('button',{name:'開啟採購單 R8-PO',exact:true}).click();await expect(page.getByTestId('po-total')).toHaveText('300');expect(await json('/api/purchase-orders/'+source.id)).toEqual(originalOrder)
 for(const [hash,title] of [['raw-materials','原料維護'],['finished-goods','成品資料維護'],['suppliers','供應商資料維護'],['supplier-offers','供應商供料設定']]){await page.goto('/#'+hash);await expect(page.getByRole('heading',{name:title,exact:true})).toBeVisible()}
 await page.goto('/#purchase-receipts');await page.getByRole('button',{name:'開啟入庫單 R8-RC-MOBILE',exact:true}).click();await expect(page.getByTestId('rc-state')).toHaveText('已過帳 · 唯讀');expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);evidence.mobile={stock,sql:sqlState(),noHorizontalOverflow:true,adjacentNegativeFeedback:true,noLotRequired:true};evidence.finalOrder=await json('/api/purchase-orders/'+source.id)
})
