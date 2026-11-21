import {test,expect,type Page} from '@playwright/test'
import {spawn,execFileSync,type ChildProcess} from 'node:child_process'
import {mkdirSync,createWriteStream,writeFileSync} from 'node:fs'
import path from 'node:path'
import {fileURLToPath} from 'node:url'
import {randomUUID} from 'node:crypto'

const root=path.resolve(fileURLToPath(new URL('../../',import.meta.url)))
const task=path.resolve(root,process.env.ERP_CUSTOMER_RETURN_QA_DIR||'artifacts/round21-2026-11-20T11-15-22-039Z')
const out=path.resolve(root,process.env.ERP_TEST_ARTIFACTS||path.relative(root,path.join(task,'desktop')))
mkdirSync(out,{recursive:true})
const py=path.join(root,'.venv','Scripts','python.exe'),database=path.join(task,'customer-return-desktop-'+randomUUID()+'.sqlite')
const api='http://127.0.0.1:8037',ui='http://127.0.0.1:5200',API='/api/customer-returns'
type Runtime={child:ChildProcess}
let runtime:Runtime|undefined,fixtures:any,initialRows:any,baseline:any,first:any,second:any,over:any,retryBody:any,firstFinalization:any
const evidence:any={desktopOnly:true,mobileTests:0,ownerDatabaseUsed:false,pageErrors:[],cancelMutationCounts:[],stops:[],finalizationRequests:[]}
test.use({baseURL:ui,viewport:{width:1440,height:1100},isMobile:false,trace:'off',screenshot:'off'})
test.describe.configure({mode:'serial'})

function ps(script:string){return JSON.parse(execFileSync('powershell.exe',['-NoProfile','-Command',script],{windowsHide:true,encoding:'utf8',stdio:['ignore','pipe','pipe']}))}
function listeners(){return ps('ConvertTo-Json -InputObject @(Get-NetTCPConnection -State Listen | Where-Object LocalPort -in 8037,5200 | Select-Object LocalAddress,LocalPort,OwningProcess) -Compress')}
async function ready(url:string){await expect.poll(async()=>{try{return(await fetch(url,{signal:AbortSignal.timeout(1000)})).status}catch{return 0}},{timeout:45_000}).toBe(200)}
async function start(){
 expect(listeners()).toEqual([])
 const child=spawn(py,['-u','-m','backend.local_demo','--database',database,'--backend-port','8037','--frontend-port','5200'],{cwd:root,windowsHide:true,stdio:['ignore','pipe','pipe']})
 child.stdout?.pipe(createWriteStream(path.join(out,'runtime.log'),{flags:'a'}));child.stderr?.pipe(createWriteStream(path.join(out,'runtime-error.log'),{flags:'a'}))
 runtime={child};await ready(api+'/api/health');await ready(ui)
}
async function stop(){
 const child=runtime?.child;if(!child?.pid||child.exitCode!==null)return
 const pid=child.pid
 const tree=ps('$qaProcesses=@(Get-CimInstance Win32_Process);$qaIds=[System.Collections.Generic.HashSet[int]]::new();$qaQueue=[System.Collections.Generic.Queue[int]]::new();$qaQueue.Enqueue('+pid+');while($qaQueue.Count){$qaNext=$qaQueue.Dequeue();if($qaIds.Add($qaNext)){foreach($qaChild in @($qaProcesses|Where-Object ParentProcessId -eq $qaNext)){$qaQueue.Enqueue([int]$qaChild.ProcessId)}}};ConvertTo-Json -InputObject @($qaProcesses|Where-Object {$qaIds.Contains([int]$_.ProcessId)}|Select-Object ProcessId,ParentProcessId,ExecutablePath,CommandLine) -Compress')
 const parent=tree.find((p:any)=>p.ProcessId===pid)
 expect(parent.ExecutablePath.toLowerCase()).toBe(py.toLowerCase());expect(parent.CommandLine).toContain('backend.local_demo');expect(parent.CommandLine).toContain(database)
 const sockets=listeners(),ids=new Set(tree.map((p:any)=>p.ProcessId));expect(sockets).toHaveLength(2);expect(sockets.every((s:any)=>s.LocalAddress==='127.0.0.1'&&ids.has(s.OwningProcess))).toBe(true)
 execFileSync('taskkill.exe',['/PID',String(pid),'/T','/F'],{windowsHide:true,stdio:'pipe'})
 await new Promise<void>(resolve=>{if(child.exitCode!==null||child.signalCode!==null)resolve();else child.once('exit',()=>resolve())})
 await expect.poll(listeners).toEqual([]);evidence.stops.push({before:sockets,after:[]});runtime=undefined
}
async function json(url:string){const response=await fetch(api+url,{headers:{Connection:'close'}});expect(response.status).toBe(200);return response.json()}
function rows(){return JSON.parse(execFileSync(py,['-c','import json,sys;from backend.tests.test_supplier_offers import snapshot;print(json.dumps(snapshot(sys.argv[1])["rows"]))',database],{cwd:root,windowsHide:true,encoding:'utf8',stdio:['ignore','pipe','pipe']}))}
async function originalDataPreserved(){const after=rows();for(const [name,value] of Object.entries(initialRows))if(!name.startsWith('customer_return')&&name!=='stock_create_receipt')expect(after[name],name).toEqual(value);for(const [name,value] of Object.entries(baseline))expect(await json('/api/'+name),name).toEqual(value)}
async function source(){return(await json(API+'/sources')).find((s:any)=>s.shipment_id===fixtures.shipment.id)}
async function fgStock(){return(await json('/api/stock-positions')).find((p:any)=>p.id===fixtures.position.id).on_hand_qty_base}
async function rawStock(){return(await json('/api/stock-positions')).find((p:any)=>p.item_id===fixtures.raws[0].id&&p.lot_code==='LOT-R8-001').on_hand_qty_base}
async function grossShipmentPreserved(){const shipment=await json('/api/sales-shipments/'+fixtures.shipment.id);const {current_stock_qty_base:unused,...actual}=shipment;const {current_stock_qty_base:initial,...expected}=fixtures.shipment;expect(actual).toEqual(expected);expect(shipment.quantity).toBe('0.75');expect(shipment.movements.map((m:any)=>m.qty_base_delta)).toEqual(['-0.75']);expect(shipment.source.shipped_qty_base).toBe('0.75');expect(shipment.source.remaining_qty_base).toBe('0.5')}
function watch(page:Page){page.on('pageerror',e=>evidence.pageErrors.push(e.message));page.on('request',r=>{if(r.method()==='POST'&&r.url().endsWith('/finalize'))evidence.finalizationRequests.push(r.url())})}
async function open(page:Page,row=first){await page.goto('/#customer-returns?record='+row.id);await expect(page.getByTestId('cr-number-value')).toHaveText(row.document_no)}
async function create(page:Page,number:string,quantity:string,restock:boolean){
 await page.getByRole('button',{name:'＋ 新增客戶退貨單',exact:true}).click()
 await page.getByLabel('客戶退貨單號 *',{exact:true}).fill(number)
 await page.getByLabel('來源銷售出貨單 *',{exact:true}).selectOption(fixtures.shipment.id)
 await expect(page.getByTestId('cr-source-customer')).toContainText(fixtures.customers[0].code)
 await expect(page.getByLabel('是否確認可回庫 *',{exact:true})).toHaveValue('')
 await page.getByLabel('退貨日期 *',{exact:true}).fill('2026-11-20')
 await page.getByLabel('本次退貨數量 *',{exact:true}).fill(quantity)
 await page.getByLabel('是否確認可回庫 *',{exact:true}).selectOption(restock?'true':'false')
 await page.getByLabel('退貨原因',{exact:true}).fill('隔離桌面驗證：人工指定本次處理方式')
 await page.getByLabel('備註',{exact:true}).fill('第二十一輪獨立桌面退貨驗收')
 await page.getByRole('button',{name:'儲存退貨草稿',exact:true}).click()
 await expect(page.getByTestId('cr-number-value')).toHaveText(number)
 return(await json(API+'?q='+encodeURIComponent(number)))[0]
}
async function zeroMutation(page:Page,action:()=>Promise<void>){const mutations:string[]=[];const listener=(r:any)=>{if(!['GET','HEAD'].includes(r.method()))mutations.push(r.method()+' '+r.url())};page.on('request',listener);await action();await page.waitForTimeout(200);page.off('request',listener);expect(mutations).toEqual([]);evidence.cancelMutationCounts.push(mutations.length)}
test.beforeAll(async()=>{
 const fixturePath=path.join(out,'fixtures.json')
 const seedCode='import json,sys;from pathlib import Path;from backend.tests.seed_customer_returns_browser import seed;metadata=seed(Path(sys.argv[1]));Path(sys.argv[2]).write_text(json.dumps(metadata,ensure_ascii=False,indent=2)+"\\n",encoding="utf8");print(json.dumps(metadata,ensure_ascii=False))'
 fixtures=JSON.parse(execFileSync(py,['-c',seedCode,database,fixturePath],{cwd:root,windowsHide:true,encoding:'utf8',env:{...process.env,PYTHONIOENCODING:'utf-8'},stdio:['ignore','pipe','pipe']}))
 await start();initialRows=rows();const names=['sales-orders','work-orders','boms','routings','operation-definitions','purchase-orders','purchase-outstanding','purchase-receipts','supplier-returns','material-issues','material-returns','production-completions']
 baseline=Object.fromEntries(await Promise.all(names.map(async k=>[k,await json('/api/'+k)])))
 expect(await fgStock()).toBe('0.5');expect(await rawStock()).toBe('3');expect((await source()).original_shipped_qty_base).toBe('0.75');expect((await source()).finalized_returned_qty_base).toBe('0');expect((await source()).remaining_returnable_qty_base).toBe('0.75')
 evidence.initialStock={finished:'0.5',raw:'3'}
})
test.afterAll(async()=>{if(runtime)await stop();writeFileSync(path.join(out,'customer-return-desktop-evidence.json'),JSON.stringify(evidence,null,2)+'\n')})

test('A — real shipment-linked draft, explicit designation and Cancel/Escape/F5 do not finalize',async({page})=>{
 watch(page);await page.goto('/#sales-shipments');await page.getByRole('button',{name:'客戶退貨管理',exact:true}).click();await expect(page.getByRole('heading',{name:'客戶退貨管理',exact:true})).toBeVisible()
 first=await create(page,'CRET-QA-DESKTOP-A','0.25',true);expect(first.finalization_state).toBe('DRAFT');expect(first.source.shipment_id).toBe(fixtures.shipment.id)
 await zeroMutation(page,async()=>{await page.getByRole('button',{name:'編輯退貨單',exact:true}).click();await page.getByLabel('本次退貨數量 *',{exact:true}).fill('0.5');await page.getByRole('button',{name:'取消',exact:true}).click()})
 await expect(page.getByTestId('cr-quantity-value')).toContainText('0.25')
 await zeroMutation(page,async()=>{await page.getByRole('button',{name:'編輯退貨單',exact:true}).click();await page.getByLabel('備註',{exact:true}).fill('discard');await page.keyboard.press('Escape')})
 await zeroMutation(page,async()=>{await page.getByRole('button',{name:'檢視客戶退貨確認',exact:true}).click();await expect(page.getByTestId('cr-review-current-stock')).toContainText('0.5');await expect(page.getByTestId('cr-expected-stock')).toContainText('0.75');await expect(page.getByTestId('cr-review-remaining-after')).toContainText('0.5');await page.keyboard.press('Escape');await expect(page.getByRole('dialog')).not.toBeVisible()})
 await page.reload();await expect(page.getByTestId('cr-number-value')).toHaveText(first.document_no);expect(await fgStock()).toBe('0.5');expect(await rawStock()).toBe('3');expect((await source()).finalized_returned_qty_base).toBe('0');await originalDataPreserved();await grossShipmentPreserved()
 await page.screenshot({path:path.join(out,'draft-no-finalization.png'),fullPage:true});evidence.draft={quantity:'0.25',stock:'0.5',returned:'0',f5:true,cancelMutationCounts:[...evidence.cancelMutationCounts]}
})

test('B — explicit restock +0.25 and lost-response retry restore the original position exactly once',async({page})=>{
 watch(page);await open(page);await page.getByRole('button',{name:'檢視客戶退貨確認',exact:true}).click();await expect(page.getByTestId('cr-review-source-returned')).toContainText('0 ');await expect(page.getByTestId('cr-expected-stock')).toContainText('0.75')
 const finalizeUrl='**/api/customer-returns/'+first.id+'/finalize'
 await page.route(finalizeUrl,async route=>{retryBody=route.request().postDataJSON();const response=await route.fetch();expect(response.status()).toBe(201);firstFinalization=await response.json();await route.abort('failed')})
 await page.getByRole('button',{name:'確認退貨並增加成品庫存',exact:true}).click();await expect(page.getByRole('dialog').getByRole('alert')).toContainText('尚未取得成功回應');await page.unroute(finalizeUrl)
 expect(await fgStock()).toBe('0.75');expect((await source()).finalized_returned_qty_base).toBe('0.25');expect((await source()).remaining_returnable_qty_base).toBe('0.5')
 const response=page.waitForResponse(r=>r.url().endsWith('/'+first.id+'/finalize')&&r.request().method()==='POST');await page.getByRole('button',{name:'確認退貨並增加成品庫存',exact:true}).click();expect((await response).status()).toBe(200)
 await expect(page.getByTestId('cr-state')).toContainText('POSTED');await expect(page.getByTestId('cr-movement')).toContainText('+0.25');await expect(page.getByTestId('cr-current-stock')).toContainText('0.75');expect(await fgStock()).toBe('0.75')
 const posted=await json(API+'/'+first.id);expect(posted.movements).toHaveLength(1);expect(posted.finalization_id).toBe(firstFinalization.finalization_id);expect(posted.movements[0].position_id).toBe(fixtures.position.id);expect(posted.movements[0].lot_id).toBe(fixtures.position.lot_id)
 expect((await json('/api/sales-orders/'+fixtures.order.id)).total_amount).toBe('68.125');expect(await rawStock()).toBe('3');await originalDataPreserved();await grossShipmentPreserved()
 await expect(page.getByRole('button',{name:'編輯退貨單',exact:true})).toHaveCount(0);await expect(page.getByRole('button',{name:'刪除退貨草稿',exact:true})).toHaveCount(0)
 await page.screenshot({path:path.join(out,'restocked-plus-025.png'),fullPage:true});evidence.firstRestock={movement:'+0.25',stockBefore:'0.5',stockAfter:'0.75',returned:'0.25',remaining:'0.5',grossShipped:'0.75',sum:'68.125',lostResponseReplayStatus:200,movements:1}
})

test('C — record-only partial consumes the shared return cap without stock, excess rejects, F5/full restart/replay persist',async({page})=>{
 watch(page);await open(page);second=await create(page,'CRET-QA-DESKTOP-NO-RESTOCK','0.5',false);await page.getByRole('button',{name:'檢視客戶退貨確認',exact:true}).click();await expect(page.getByTestId('cr-review-source-returned')).toContainText('0.25');await expect(page.getByTestId('cr-expected-stock')).toContainText('0.75');await expect(page.getByRole('button',{name:'確認退貨並增加成品庫存',exact:true})).toHaveCount(0);await page.getByRole('button',{name:'確認登記退貨（不回庫）',exact:true}).click()
 await expect(page.getByTestId('cr-state')).toContainText('RECORDED_NO_RESTOCK');await expect(page.getByTestId('cr-no-movement')).toContainText('零庫存分錄');expect((await json(API+'/'+second.id)).movements).toEqual([])
 expect(await fgStock()).toBe('0.75');expect((await source()).finalized_returned_qty_base).toBe('0.75');expect((await source()).remaining_returnable_qty_base).toBe('0')
 over=await create(page,'CRET-QA-DESKTOP-EXCESS','0.1',true);await page.getByRole('button',{name:'檢視客戶退貨確認',exact:true}).click();await expect(page.getByRole('alert')).toContainText(/剩餘|可退|超過/);await expect(page.getByRole('alert').locator('..').getByRole('button',{name:'檢視客戶退貨確認',exact:true})).toBeVisible();expect((await json(API+'/'+over.id)).finalization_state).toBe('DRAFT');expect(await fgStock()).toBe('0.75')
 await open(page,first);await zeroMutation(page,async()=>{await page.getByRole('button',{name:'重新讀取來源與庫存',exact:true}).click();await expect(page.getByTestId('cr-current-stock')).toContainText('0.75');await expect(page.getByTestId('cr-source-returned')).toContainText('0.75');await expect(page.getByTestId('cr-source-remaining')).toContainText('0 ')})
 await page.getByLabel('搜尋客戶退貨單',{exact:true}).fill(fixtures.shipment.document_no);await page.getByRole('button',{name:'搜尋',exact:true}).click();await expect(page.getByRole('button',{name:'開啟客戶退貨單 '+first.document_no,exact:true})).toBeVisible();await page.reload();await expect(page.getByTestId('cr-movement')).toContainText('+0.25')
 const beforeFirst=await json(API+'/'+first.id),beforeSecond=await json(API+'/'+second.id),beforeDraft=await json(API+'/'+over.id),oldPids=listeners().map((s:any)=>s.OwningProcess)
 await stop();await start();const newPids=listeners().map((s:any)=>s.OwningProcess);expect(newPids.every((n:number)=>!oldPids.includes(n))).toBe(true)
 await page.reload();await expect(page.getByTestId('cr-state')).toContainText('POSTED');expect(await json(API+'/'+first.id)).toEqual(beforeFirst);expect(await json(API+'/'+second.id)).toEqual(beforeSecond);expect(await json(API+'/'+over.id)).toEqual(beforeDraft)
 const replay=await fetch(api+API+'/'+first.id+'/finalize',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(retryBody)});expect(replay.status).toBe(200);expect((await replay.json()).finalization_id).toBe(firstFinalization.finalization_id)
 expect(await fgStock()).toBe('0.75');expect(await rawStock()).toBe('3');expect((await source()).remaining_returnable_qty_base).toBe('0');expect((await json('/api/sales-orders/'+fixtures.order.id)).total_amount).toBe('68.125');await originalDataPreserved();await grossShipmentPreserved();expect(evidence.pageErrors).toEqual([])
 await page.screenshot({path:path.join(out,'restart-record-only-cap-exhausted.png'),fullPage:true});evidence.recordOnly={quantity:'0.5',movements:0,stock:'0.75',finalizedReturned:'0.75',remaining:'0',excessRejected:true,excessStillDraft:true};evidence.restart={oldPids,newPids,oldListenersAbsent:true,originalReplayStatus:200,postedRecordedAndDraftPreserved:true};evidence.finalStock={finished:'0.75',raw:'3'};evidence.result='PASS'
})
