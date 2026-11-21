import {test,expect,type Page} from '@playwright/test'
import {spawn,execFileSync,type ChildProcess} from 'node:child_process'
import {mkdirSync,createWriteStream,writeFileSync} from 'node:fs'
import path from 'node:path'
import {fileURLToPath} from 'node:url'
import {randomUUID} from 'node:crypto'
const root=path.resolve(fileURLToPath(new URL('../../',import.meta.url)))
const artifacts=path.resolve(root,process.env.ERP_TEST_ARTIFACTS||'artifacts/round18-2026-11-20T03-24-10-225Z/browser/results'),task=path.dirname(artifacts)
mkdirSync(artifacts,{recursive:true})
const py=path.join(root,'.venv','Scripts','python.exe'),apiUrl='http://127.0.0.1:8032',uiUrl='http://127.0.0.1:5195'
const database=path.join(task,'finished-goods-'+randomUUID()+'.sqlite')
const API_PATH='/api/finished-goods-receipts'
type Runtime={child:ChildProcess;ports:number[];marker:string;name:string}
let runtime:Runtime,fixtures:any,draft:any,retryBody:any,posted:any,persistDraft:any,baseline:any,initial:any,completionSources:any[]
const evidence:any={desktopOnly:true,mobileTests:0,ownerDatabaseUsed:false,pageErrors:[],stops:[],cancelMutationCounts:[],postingRequests:0}
test.use({baseURL:uiUrl,viewport:{width:1440,height:1100},isMobile:false,screenshot:'off',trace:'off'})
test.describe.configure({mode:'serial'})
function ps(script:string){return JSON.parse(execFileSync('powershell.exe',['-NoProfile','-Command',script],{windowsHide:true,encoding:'utf8',stdio:['ignore','pipe','pipe']}))}
function listeners(ports:number[]){return ps('ConvertTo-Json -InputObject @(Get-NetTCPConnection -State Listen | Where-Object LocalPort -in '+ports.join(',')+' | Select-Object LocalAddress,LocalPort,OwningProcess) -Compress')}
async function ready(url:string){await expect.poll(async()=>{try{return(await fetch(url,{signal:AbortSignal.timeout(1000)})).status}catch{return 0}},{timeout:45_000}).toBe(200)}
async function offline(url:string){await expect.poll(async()=>{try{return(await fetch(url,{signal:AbortSignal.timeout(1000)})).status}catch{return 0}},{timeout:20_000}).toBe(0)}
function ownership(r:Runtime){
 const pid=Number(r.child.pid)
 const tree=ps('$qaProcesses=Get-CimInstance Win32_Process; $qaIds=@('+pid+'); do {$qaChildren=@($qaProcesses | Where-Object {$_.ParentProcessId -in $qaIds -and $_.ProcessId -notin $qaIds} | Select-Object -ExpandProperty ProcessId); $qaIds+=$qaChildren}while($qaChildren.Count -gt 0); ConvertTo-Json -InputObject @($qaProcesses | Where-Object ProcessId -in $qaIds | Select-Object ProcessId,ParentProcessId,ExecutablePath,CommandLine) -Depth 5 -Compress')
 const parent=tree.find((p:any)=>p.ProcessId===pid)
 if(!parent||String(parent.ExecutablePath).toLowerCase()!==py.toLowerCase()||!String(parent.CommandLine).includes(r.marker)||!String(parent.CommandLine).toLowerCase().includes(database.toLowerCase()))throw Error('Finished-goods QA launcher ownership mismatch; nothing stopped')
 const sockets=listeners(r.ports),ids=new Set(tree.map((p:any)=>p.ProcessId))
 if(sockets.length!==2||sockets.some((s:any)=>s.LocalAddress!=='127.0.0.1'||!ids.has(s.OwningProcess)))throw Error('Finished-goods QA listener ownership mismatch; nothing stopped')
 return {pid,tree,listeners:sockets,database,runtime:r.name}
}
async function start(demo=true){
 const ports=demo?[8032,5195]:[8033,5196],name=demo?'local-demo':'ordinary',marker=demo?'backend.local_demo':'scripts/dev.py'
 expect(listeners(ports)).toEqual([])
 const args=demo?['-u','-m','backend.local_demo','--database',database,'--backend-port',String(ports[0]),'--frontend-port',String(ports[1])]:['-u','scripts/dev.py','--database',database,'--backend-port',String(ports[0]),'--frontend-port',String(ports[1])]
 const child=spawn(py,args,{cwd:root,windowsHide:true,stdio:['ignore','pipe','pipe']})
 child.stdout?.pipe(createWriteStream(path.join(artifacts,name+'-launcher.log'),{flags:'a'}));child.stderr?.pipe(createWriteStream(path.join(artifacts,name+'-launcher-error.log'),{flags:'a'}))
 const result={child,ports,marker,name}
 await ready('http://127.0.0.1:'+ports[0]+'/api/health');await ready('http://127.0.0.1:'+ports[1]);return result
}
async function stop(r:Runtime){
 const before=ownership(r)
 execFileSync('taskkill.exe',['/PID',String(r.child.pid),'/T','/F'],{windowsHide:true,stdio:'pipe'})
 await new Promise<void>(resolve=>{if(r.child.exitCode!==null||r.child.signalCode!==null)resolve();else r.child.once('exit',()=>resolve())})
 await offline('http://127.0.0.1:'+r.ports[0]+'/api/health');await offline('http://127.0.0.1:'+r.ports[1]);expect(listeners(r.ports)).toEqual([])
 evidence.stops.push({before,afterListeners:[]})
}
function snapshot(){return JSON.parse(execFileSync(py,['-c','import json,sys;from backend.tests.test_finished_goods_receipts import snapshot;print(json.dumps(snapshot(sys.argv[1])))',database],{cwd:root,windowsHide:true,encoding:'utf8',stdio:['ignore','pipe','pipe']}))}
async function json(url:string){const r=await fetch(apiUrl+url,{headers:{Connection:'close'}});expect(r.status).toBe(200);return r.json()}
async function rawStock(){return(await json('/api/stock-positions')).find((p:any)=>p.item_id===fixtures.raws[0].id&&p.lot_code==='LOT-R8-001').on_hand_qty_base}
async function fgStock(index:number,lot:string|null){const completion=fixtures.completions[index];return(await json('/api/stock-positions')).find((p:any)=>p.item_id===completion.product_id&&p.warehouse_id===fixtures.warehouse.id&&p.lot_code===lot)?.on_hand_qty_base??'0'}
async function source(index:number){const id=fixtures.completions[index].id;return(await json(API_PATH+'/sources?completion_id='+id)).find((s:any)=>s.completion_id===id)}
async function existing(){return Object.fromEntries(await Promise.all(['material-issues','material-returns','work-orders','production-completions','purchase-orders','purchase-outstanding','purchase-receipts','supplier-returns','boms','routings','operation-definitions'].map(async k=>[k,await json('/api/'+k)])))}
function watch(page:Page){page.on('pageerror',e=>evidence.pageErrors.push(e.message))}
async function open(page:Page,row=draft){await page.goto('/#finished-goods-receipts?record='+row.id);await expect(page.getByTestId('fgr-number-value')).toHaveText(row.document_no)}
async function create(page:Page,number:string,quantity='1.25',index=0,lot='LOT-FG-R18-001',existingLotId?:string){
 await page.getByRole('button',{name:'＋ 新增成品入庫單',exact:true}).click()
 await page.getByLabel('成品入庫單號 *',{exact:true}).fill(number)
 await page.getByLabel('已確認完工回報 *',{exact:true}).selectOption(fixtures.completions[index].id)
 await page.getByLabel('入庫日期 *',{exact:true}).fill('2026-11-20')
 await page.getByLabel('收貨倉庫 *',{exact:true}).selectOption(fixtures.warehouse.id)
 await page.getByLabel('本次入庫量 *',{exact:true}).fill(quantity)
 const tracked=completionSources.find(s=>s.completion_id===fixtures.completions[index].id).lot_tracking
 if(tracked){if(existingLotId){await page.getByLabel('成品批次選擇',{exact:true}).selectOption('existing');await page.getByLabel('成品庫存批號 *',{exact:true}).selectOption(existingLotId)}else await page.getByLabel('成品庫存批號 *',{exact:true}).fill(lot)}
 else await expect(page.getByLabel('成品庫存批號',{exact:true})).toBeDisabled()
 await page.getByLabel('備註',{exact:true}).fill('Synthetic desktop finished-goods receiving QA')
 const waiting=page.waitForResponse(r=>r.url().endsWith(API_PATH)&&r.request().method()==='POST')
 await page.getByRole('button',{name:'儲存入庫草稿',exact:true}).click()
 const response=await waiting;expect(response.status()).toBe(201);const row=await response.json()
 await expect(page.getByTestId('fgr-number-value')).toHaveText(number);return row
}
async function review(page:Page){await page.getByRole('button',{name:'檢視成品入庫過帳',exact:true}).click();const modal=page.getByRole('dialog',{name:'檢視成品入庫過帳',exact:true});await expect(modal).toBeVisible();return modal}
async function post(page:Page){const modal=await review(page);const waiting=page.waitForResponse(r=>r.url().endsWith('/post')&&r.request().method()==='POST');await modal.getByRole('button',{name:'確認並增加成品庫存',exact:true}).click();const response=await waiting;expect(response.status()).toBe(201);await expect(modal).not.toBeVisible();await expect(page.getByTestId('fgr-state')).toContainText('POSTED');return response.json()}
function inventoryRows(){const data=snapshot();return Object.fromEntries(['inventory_lot','stock_position','stock_entry','stock_posting','finished_goods_posting','finished_goods_entry'].map(t=>[t,data.rows[t]]))}
function assertOriginalDataUnchanged(){
 const after=snapshot(),allowed=new Set(['inventory_lot','stock_position','stock_create_receipt','finished_goods_receipt','finished_goods_posting','finished_goods_entry'])
 for(const [table,rows] of Object.entries(initial.rows))if(!allowed.has(table))expect(after.rows[table]).toEqual(rows)
 for(const table of ['inventory_lot','stock_position'])for(const row of initial.rows[table])expect(after.rows[table]).toContainEqual(row)
 expect(after.objects).toEqual(initial.objects)
}

test.beforeAll(async()=>{
 expect(listeners([8032,5195])).toEqual([])
 const seedCode=`import json,sys
from pathlib import Path
from uuid import uuid4
from backend.app.local_demo import create_local_demo_app
from backend.tests.test_finished_goods_receipts import seed_finished_goods_receipt_qa_database,client_for,add_completion
path=Path(sys.argv[1]);metadata=seed_finished_goods_receipt_qa_database(path)
# The full-chain fixture's original second product is actually tracked. Build
# a genuine separate untracked product/order/report rather than renaming it.
app=create_local_demo_app(path,backend_port=8032,frontend_port=5195)
with client_for(app) as client:
 original=metadata['products'][0]
 response=client.post('/api/finished-goods',json=dict(code='FG-NOLOT-DESKTOP',name='Desktop untracked finished product',category_id=original['category_id'],base_unit_id=original['base_unit_id'],lot_tracking=False,is_active=True))
 assert response.status_code==201,response.text
 product=response.json();metadata['products'].append(product)
 response=client.post('/api/work-orders',json=dict(request_key=str(uuid4()),document_no='WO-NOLOT-DESKTOP',product_id=product['id'],planned_qty_base='3.75',planned_start='2026-11-20',planned_finish='2026-11-25'))
 assert response.status_code==201,response.text
 metadata['orders'].append(response.json())
 context=dict(metadata,client=client,app=app,db=app.state.db,path=path)
 metadata['original_tracked_second_completion']=metadata['completions'][1]
 metadata['completions'][1]=add_completion(context,'COMP-NOLOT-DESKTOP',order=len(metadata['orders'])-1)
print(json.dumps(metadata,ensure_ascii=False))`
 fixtures=JSON.parse(execFileSync(py,['-c',seedCode,database],{cwd:root,windowsHide:true,encoding:'utf8',env:{...process.env,PYTHONIOENCODING:'utf-8'},stdio:['ignore','pipe','pipe']}))
 writeFileSync(path.join(artifacts,'fixture-description.json'),JSON.stringify(fixtures,null,2))
 runtime=await start();evidence.initialOwnership=ownership(runtime);baseline=await existing();initial=snapshot();completionSources=await json(API_PATH+'/sources')
 expect(await rawStock()).toBe('3');expect(await json(API_PATH)).toEqual([])
 expect(completionSources.find(s=>s.completion_id===fixtures.completions[1].id).lot_tracking).toBe(false)
 expect([completionSources.find(s=>s.completion_id===fixtures.completions[0].id).good_qty_base,completionSources.find(s=>s.completion_id===fixtures.completions[0].id).defective_qty_base]).toEqual(['1.25','0.25'])
})
test.afterEach(()=>expect(evidence.pageErrors).toEqual([]))
test.afterAll(async()=>{try{if(runtime&&runtime.child.exitCode===null&&runtime.child.signalCode===null)await stop(runtime)}finally{writeFileSync(path.join(artifacts,'finished-goods-desktop-evidence.json'),JSON.stringify(evidence,null,2))}})

test('Desktop A: draft editing, zero-mutation Cancel, search/F5 and no early inventory objects',async({page})=>{
 watch(page);await page.goto('/#finished-goods-receipts');await page.getByRole('button',{name:'＋ 新增成品入庫單',exact:true}).waitFor()
 let mutations=0;const count=(r:any)=>{if(['POST','PUT','DELETE','PATCH'].includes(r.method()))mutations++}
 page.on('request',count);const before=snapshot()
 await page.getByLabel('成品入庫單號 *',{exact:true}).fill('UNSAVED');await page.getByRole('button',{name:'取消',exact:true}).click()
 expect(mutations).toBe(0);expect(snapshot()).toEqual(before);page.off('request',count)
 const inventory=inventoryRows();draft=await create(page,'FGRCV-TEST-001','0.5')
 await page.getByRole('button',{name:'編輯入庫單',exact:true}).click();await page.getByLabel('本次入庫量 *',{exact:true}).fill('1.25')
 const saved=page.waitForResponse(r=>r.request().method()==='PUT'&&r.url().endsWith('/'+draft.id));await page.getByRole('button',{name:'儲存變更',exact:true}).click();draft=await(await saved).json()
 await expect(page.getByTestId('fgr-quantity-value')).toContainText('1.25')
 const persisted=snapshot();page.on('request',count)
 await page.getByRole('button',{name:'編輯入庫單',exact:true}).click();await page.getByLabel('本次入庫量 *',{exact:true}).fill('9.99');await page.getByRole('button',{name:'取消',exact:true}).click()
 expect(mutations).toBe(0);expect(snapshot()).toEqual(persisted);page.off('request',count);evidence.cancelMutationCounts.push(mutations)
 await page.getByLabel('搜尋成品入庫單',{exact:true}).fill('FGRCV-TEST-001');await page.getByRole('button',{name:'搜尋',exact:true}).click();await expect(page.getByRole('button',{name:'開啟成品入庫單 FGRCV-TEST-001',exact:true})).toBeVisible()
 await page.reload();await expect(page.getByTestId('fgr-number-value')).toHaveText('FGRCV-TEST-001');await expect(page.getByTestId('fgr-quantity-value')).toContainText('1.25');await expect(page.getByTestId('fgr-current-stock')).toContainText('0 ')
 expect(inventoryRows()).toEqual(inventory);expect((await source(0)).remaining_qty_base).toBe('1.25');expect(await rawStock()).toBe('3');assertOriginalDataUnchanged()
 evidence.draft={quantity:'1.25',lotCreated:false,positionCreated:false,movementCreated:false,sourceRemaining:'1.25',rawStock:'3',f5:true}
 await page.screenshot({path:path.join(artifacts,'finished-goods-draft-desktop.png'),fullPage:true})
})

test('Desktop B: reviewed explicit +1.25 posting, defective exclusion, original replay and source integrity',async({page,playwright})=>{
 watch(page);await open(page);let mutations=0;const count=(r:any)=>{if(['POST','PUT','DELETE','PATCH'].includes(r.method()))mutations++};page.on('request',count)
 const before=snapshot();let modal=await review(page)
 await expect(modal.getByTestId('fgr-review-source-good')).toContainText('1.25 ');await expect(modal.getByTestId('fgr-review-source-defective')).toContainText('0.25 ')
 await expect(modal.getByTestId('fgr-review-current-stock')).toContainText('0 ');await expect(modal.getByTestId('fgr-expected-stock')).toContainText('1.25 ');await expect(modal.getByTestId('fgr-review-remaining-after')).toContainText('0 ')
 await modal.getByRole('button',{name:'取消',exact:true}).click();expect(snapshot()).toEqual(before)
 modal=await review(page);await page.keyboard.press('Escape');await expect(modal).not.toBeVisible();expect(mutations).toBe(0);expect(snapshot()).toEqual(before);page.off('request',count);evidence.cancelMutationCounts.push(mutations)
 const capture=(r:any)=>{if(r.method()==='POST'&&r.url().endsWith(API_PATH+'/'+draft.id+'/post')){retryBody=r.postDataJSON();evidence.postingRequests++}};page.on('request',capture)
 posted=await post(page);page.off('request',capture)
 expect(posted.authorization_basis).toBe('LOCAL_DEMO_MANUAL');expect(posted.movements).toHaveLength(1);expect(posted.movements[0].qty_base_delta).toBe('1.25')
 await expect(page.getByTestId('fgr-movement')).toContainText('+1.25');await expect(page.getByTestId('fgr-current-stock')).toContainText('1.25 ')
 await expect(page.getByRole('button',{name:'編輯入庫單',exact:true})).toHaveCount(0);await expect(page.getByRole('button',{name:'刪除入庫草稿',exact:true})).toHaveCount(0)
 const pool=await source(0);expect([pool.good_qty_base,pool.defective_qty_base,pool.received_qty_base,pool.remaining_qty_base]).toEqual(['1.25','0.25','1.25','0'])
 expect(await fgStock(0,'LOT-FG-R18-001')).toBe('1.25');expect(await rawStock()).toBe('3');expect(await existing()).toEqual(baseline);assertOriginalDataUnchanged()
 const after=snapshot(),client=await playwright.request.newContext({baseURL:apiUrl,extraHTTPHeaders:{Connection:'close'}})
 expect((await client.post(API_PATH+'/'+draft.id+'/post',{data:retryBody})).status()).toBe(200);expect(snapshot()).toEqual(after)
 expect((await client.post(API_PATH+'/'+draft.id+'/post',{data:{...retryBody,row_version:999}})).status()).toBe(409);expect(snapshot()).toEqual(after);await client.dispose()
 await page.reload();await expect(page.getByTestId('fgr-state')).toContainText('POSTED');await expect(page.getByTestId('fgr-current-stock')).toContainText('1.25 ')
 evidence.fullReceipt={source:fixtures.completions[0].id,good:'1.25',defective:'0.25',received:'1.25',remaining:'0',movement:'+1.25',finishedStock:'1.25',rawStock:'3',originalRetry:200,completionUnchanged:true,workOrderUnchanged:true}
 await page.screenshot({path:path.join(artifacts,'finished-goods-posted-desktop.png'),fullPage:true})
})

test('Desktop C: independent partial source, existing lot, excess rejection, untracked stock and complete restart',async({page,playwright})=>{
 watch(page);await open(page)
 expect((await source(2)).remaining_qty_base).toBe('1.25');expect((await source(0)).remaining_qty_base).toBe('0')
 await create(page,'FGRCV-PART-1','0.75',2,'LOT-FG-PARTIAL');const first=await post(page)
 expect([first.source.received_qty_base,first.source.remaining_qty_base]).toEqual(['0.75','0.5']);expect(await fgStock(2,'LOT-FG-PARTIAL')).toBe('0.75')
 await expect(page.getByRole('button',{name:'重新讀取來源與倉庫',exact:true})).toBeEnabled()
 const lot=(await json(API_PATH+'/lots?completion_id='+fixtures.completions[2].id)).find((l:any)=>l.lot_code==='LOT-FG-PARTIAL');expect(lot).toBeTruthy()
 const otherPage=await page.context().newPage();watch(otherPage);await otherPage.goto(uiUrl+'/#finished-goods-receipts')
 await create(otherPage,'FGRCV-PART-2','0.5',2,'LOT-FG-PARTIAL',lot.id);const second=await post(otherPage)
 await expect(otherPage.getByRole('button',{name:'重新讀取來源與倉庫',exact:true})).toBeEnabled();await otherPage.close()
 expect([second.source.received_qty_base,second.source.remaining_qty_base]).toEqual(['1.25','0']);expect(await fgStock(2,'LOT-FG-PARTIAL')).toBe('1.25');expect(second.movements[0].position_id).toBe(first.movements[0].position_id)
 // The first page still has its pre-concurrent-post record; a source refresh
 // must re-read the actual source pool and stock without making a mutation.
 await expect(page.getByTestId('fgr-source-received')).toContainText('0.75 ')
 let refreshMutations=0;const countRefresh=(r:any)=>{if(['POST','PUT','DELETE','PATCH'].includes(r.method()))refreshMutations++};page.on('request',countRefresh)
 await page.getByRole('button',{name:'重新讀取來源與倉庫',exact:true}).click()
 await expect(page.getByTestId('fgr-source-received')).toContainText('1.25 ');await expect(page.getByTestId('fgr-source-remaining')).toContainText('0 ');await expect(page.getByTestId('fgr-current-stock')).toContainText('1.25 ')
 await expect(page.getByRole('button',{name:'重新讀取來源與倉庫',exact:true})).toBeEnabled();page.off('request',countRefresh);expect(refreshMutations).toBe(0);evidence.readonlySourceRefresh={receivedBefore:'0.75',receivedAfter:'1.25',remainingAfter:'0',stockAfter:'1.25',mutationRequests:0}
 const beforeExcess=inventoryRows();const invalid=await create(page,'FGRCV-EXCESS','0.0000000000000000001',2,'LOT-FG-PARTIAL',lot.id)
 await page.getByRole('button',{name:'檢視成品入庫過帳',exact:true}).click();const alert=page.getByRole('alert');await expect(alert).toContainText(/可入庫|良品|剩餘|超過/);expect(await alert.evaluate(e=>!!e.closest('.form-actions'))).toBe(true);await expect(page.getByRole('dialog',{name:'檢視成品入庫過帳',exact:true})).not.toBeVisible()
 expect(inventoryRows()).toEqual(beforeExcess);expect((await source(2)).remaining_qty_base).toBe('0')
 page.once('dialog',d=>d.accept());await page.getByRole('button',{name:'刪除入庫草稿',exact:true}).click();await expect(page.getByRole('button',{name:'開啟成品入庫單 '+invalid.document_no,exact:true})).toHaveCount(0)
 const lotsBefore=snapshot().rows.inventory_lot
 const untracked=await create(page,'FGRCV-UNTRACKED','0.5',1);expect(untracked.lot_id).toBeNull();expect(untracked.lot_code).toBeNull();const untrackedPosted=await post(page)
 expect(untrackedPosted.movements[0].lot_id).toBeNull();expect(untrackedPosted.movements[0].lot_code).toBeNull();expect(snapshot().rows.inventory_lot).toEqual(lotsBefore);expect(await fgStock(1,null)).toBe('0.5')
 persistDraft=await create(page,'FGRCV-PERSIST-DRAFT','-0.01',2,'LOT-FG-PARTIAL',lot.id)
 expect(await rawStock()).toBe('3');expect(await existing()).toEqual(baseline);assertOriginalDataUnchanged()
 const before=snapshot(),previous=ownership(runtime);await stop(runtime);runtime=await start();const restarted=ownership(runtime)
 expect(restarted.pid).not.toBe(previous.pid);expect(restarted.listeners.every((l:any)=>!previous.listeners.some((old:any)=>old.OwningProcess===l.OwningProcess))).toBe(true)
 expect(snapshot()).toEqual(before);expect(await existing()).toEqual(baseline);expect(await rawStock()).toBe('3');expect(await fgStock(0,'LOT-FG-R18-001')).toBe('1.25');expect(await fgStock(2,'LOT-FG-PARTIAL')).toBe('1.25');expect(await fgStock(1,null)).toBe('0.5')
 await open(page,persistDraft);await expect(page.getByTestId('fgr-state')).toContainText('DRAFT');await expect(page.getByTestId('fgr-quantity-value')).toContainText('-0.01')
 await open(page,draft);await page.reload();await expect(page.getByTestId('fgr-state')).toContainText('POSTED');await expect(page.getByTestId('fgr-movement')).toContainText('+1.25')
 const client=await playwright.request.newContext({baseURL:apiUrl,extraHTTPHeaders:{Connection:'close'}});expect((await client.post(API_PATH+'/'+draft.id+'/post',{data:retryBody})).status()).toBe(200);await client.dispose();expect(snapshot()).toEqual(before)
 evidence.partials={source:fixtures.completions[2].id,quantities:['0.75','0.5'],received:'1.25',remaining:'0',stock:'1.25',samePosition:true,excessRejected:true,firstSourceIndependent:true,untrackedStock:'0.5',untrackedLotCreated:false}
 evidence.restart={previous,restarted,allOldListenersStopped:true,persistedDraft:persistDraft.id,originalReceipt:draft.id,rawStock:'3',finishedStocks:['1.25','1.25','0.5'],exactOriginalRetry:200}
 await page.screenshot({path:path.join(artifacts,'finished-goods-restart-desktop.png'),fullPage:true})
})
