import {test,expect,type Page} from '@playwright/test'
import {spawn,execFileSync,type ChildProcess} from 'node:child_process'
import {mkdirSync,createWriteStream,writeFileSync} from 'node:fs'
import path from 'node:path'
import {fileURLToPath} from 'node:url'
import {randomUUID} from 'node:crypto'
const root=path.resolve(fileURLToPath(new URL('../../',import.meta.url)))
const artifacts=path.resolve(root,process.env.ERP_TEST_ARTIFACTS||'artifacts/manual-round17/browser/results'),task=path.dirname(artifacts)
mkdirSync(artifacts,{recursive:true})
const py=path.join(root,'.venv','Scripts','python.exe'),apiUrl='http://127.0.0.1:8030',uiUrl='http://127.0.0.1:5193'
const database=path.join(task,'completion-'+randomUUID()+'.sqlite')
type Runtime={child:ChildProcess;ports:number[];marker:string;name:string}
let runtime:Runtime,fixtures:any,draft:any,retryBody:any,posted:any,persistDraft:any,baseline:any,initial:any
const evidence:any={desktopOnly:true,mobileTests:0,ownerDatabaseUsed:false,pageErrors:[],stops:[],cancelMutationCounts:[],confirmationRequests:0}
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
 if(!parent||String(parent.ExecutablePath).toLowerCase()!==py.toLowerCase()||!String(parent.CommandLine).includes(r.marker)||!String(parent.CommandLine).toLowerCase().includes(database.toLowerCase()))throw Error('Completion QA launcher ownership mismatch; nothing stopped')
 const sockets=listeners(r.ports),ids=new Set(tree.map((p:any)=>p.ProcessId))
 if(sockets.length!==2||sockets.some((s:any)=>s.LocalAddress!=='127.0.0.1'||!ids.has(s.OwningProcess)))throw Error('Completion QA listener ownership mismatch; nothing stopped')
 return {pid,tree,listeners:sockets,database,runtime:r.name}
}
async function start(demo=true){
 const ports=demo?[8030,5193]:[8031,5194],name=demo?'local-demo':'ordinary',marker=demo?'backend.local_demo':'scripts/dev.py'
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
function snapshot(){return JSON.parse(execFileSync(py,['-c','import json,sys;from backend.tests.test_supplier_offers import snapshot;print(json.dumps(snapshot(sys.argv[1])))',database],{cwd:root,windowsHide:true,encoding:'utf8',stdio:['ignore','pipe','pipe']}))}
async function json(url:string){const r=await fetch(apiUrl+url,{headers:{Connection:'close'}});expect(r.status).toBe(200);return r.json()}
async function stock(){return(await json('/api/stock-positions')).find((p:any)=>p.item_id===fixtures.raws[0].id&&p.lot_code==='LOT-R8-001').on_hand_qty_base}
const API_PATH='/api/production-completions'
async function existing(){return Object.fromEntries(await Promise.all(['stock-positions','material-issues','material-returns','work-orders','purchase-orders','purchase-outstanding','purchase-receipts','supplier-returns','boms','routings','operation-definitions'].map(async k=>[k,await json('/api/'+k)])))}
function watch(page:Page){page.on('pageerror',e=>evidence.pageErrors.push(e.message))}
async function open(page:Page,row=draft){await page.goto('/#production-completions?record='+row.id);await expect(page.getByTestId('comp-number-value')).toHaveText(row.document_no)}
async function create(page:Page,number:string,good='1.25',defective='0.25',order=0){
 await page.getByRole('button',{name:'＋ 新增完工回報',exact:true}).click()
 await page.getByLabel('完工回報單號 *',{exact:true}).fill(number)
 await page.getByLabel('製造工單 *',{exact:true}).selectOption(fixtures.orders[order].id)
 await page.getByLabel('實際回報日期 *',{exact:true}).fill('2026-11-20')
 await page.getByLabel('本次良品量 *',{exact:true}).fill(good)
 await page.getByLabel('本次不良品量 *',{exact:true}).fill(defective)
 await page.getByLabel('備註／生產說明',{exact:true}).fill('Synthetic desktop production reporting QA')
 const waiting=page.waitForResponse(r=>r.url().endsWith(API_PATH)&&r.request().method()==='POST')
 await page.getByRole('button',{name:'儲存回報草稿',exact:true}).click()
 const response=await waiting;expect(response.status()).toBe(201);const row=await response.json()
 await expect(page.getByTestId('comp-number-value')).toHaveText(number);return row
}
async function review(page:Page){await page.getByRole('button',{name:'檢視完工確認',exact:true}).click();const modal=page.getByRole('dialog',{name:'檢視完工確認',exact:true});await expect(modal).toBeVisible();return modal}
async function confirm(page:Page){const modal=await review(page);const waiting=page.waitForResponse(r=>r.url().endsWith('/confirm')&&r.request().method()==='POST');await modal.getByRole('button',{name:'確認記錄生產結果',exact:true}).click();const response=await waiting;expect(response.status()).toBe(201);await expect(modal).not.toBeVisible();await expect(page.getByTestId('comp-state')).toContainText('CONFIRMED');return response.json()}
function assertOldTablesUnchanged(){const after=snapshot();for(const [table,rows] of Object.entries(initial.rows))if(!['production_completion','stock_create_receipt'].includes(table))expect(after.rows[table]).toEqual(rows)}

test.beforeAll(async()=>{
 expect(listeners([8030,5193])).toEqual([])
 fixtures=JSON.parse(execFileSync(py,['-c','import json,sys;from pathlib import Path;from backend.tests.test_completions import seed_completion_qa_database;print(json.dumps(seed_completion_qa_database(Path(sys.argv[1])),ensure_ascii=False))',database],{cwd:root,windowsHide:true,encoding:'utf8',env:{...process.env,PYTHONIOENCODING:'utf-8'},stdio:['ignore','pipe','pipe']}))
 writeFileSync(path.join(artifacts,'fixture-description.json'),JSON.stringify(fixtures,null,2))
 runtime=await start();evidence.initialOwnership=ownership(runtime);baseline=await existing();initial=snapshot();expect(await stock()).toBe('3')
})
test.afterEach(()=>expect(evidence.pageErrors).toEqual([]))
test.afterAll(async()=>{try{if(runtime&&runtime.child.exitCode===null&&runtime.child.signalCode===null)await stop(runtime)}finally{writeFileSync(path.join(artifacts,'completion-desktop-evidence.json'),JSON.stringify(evidence,null,2))}})

test('Desktop A: draft create, quantity edit, zero-mutation Cancel, search and genuine F5',async({page})=>{
 watch(page);await page.goto('/#production-completions');await page.getByRole('button',{name:'＋ 新增完工回報',exact:true}).waitFor()
 let mutations=0;const count=(r:any)=>{if(['POST','PUT','DELETE','PATCH'].includes(r.method()))mutations++}
 page.on('request',count);const before=snapshot()
 await page.getByLabel('完工回報單號 *',{exact:true}).fill('UNSAVED');await page.getByRole('button',{name:'取消',exact:true}).click()
 expect(mutations).toBe(0);expect(snapshot()).toEqual(before);page.off('request',count)
 draft=await create(page,'COMP-TEST-001','0.5')
 await page.getByRole('button',{name:'編輯回報',exact:true}).click();await page.getByLabel('本次良品量 *',{exact:true}).fill('1.25')
 const saved=page.waitForResponse(r=>r.request().method()==='PUT'&&r.url().endsWith('/'+draft.id));await page.getByRole('button',{name:'儲存變更',exact:true}).click();draft=await(await saved).json()
 await expect(page.getByTestId('comp-good-value')).toContainText('1.25')
 const persisted=snapshot();page.on('request',count)
 await page.getByRole('button',{name:'編輯回報',exact:true}).click();await page.getByLabel('本次良品量 *',{exact:true}).fill('9.99');await page.getByRole('button',{name:'取消',exact:true}).click()
 expect(mutations).toBe(0);expect(snapshot()).toEqual(persisted);page.off('request',count);evidence.cancelMutationCounts.push(mutations)
 await page.getByLabel('搜尋完工回報',{exact:true}).fill('COMP-TEST-001');await page.getByRole('button',{name:'搜尋',exact:true}).click();await expect(page.getByRole('button',{name:'開啟完工回報 COMP-TEST-001',exact:true})).toBeVisible()
 await page.reload();await expect(page.getByTestId('comp-number-value')).toHaveText('COMP-TEST-001');await expect(page.getByTestId('comp-good-value')).toContainText('1.25');await expect(page.getByTestId('目前工單進度-good')).toContainText('0 ')
 expect(await stock()).toBe('3');assertOldTablesUnchanged();await page.screenshot({path:path.join(artifacts,'completion-draft-desktop.png'),fullPage:true})
})

test('Desktop B: explicit review, Cancel/Escape, one confirmed event, exact progress and unchanged inventories',async({page,playwright})=>{
 watch(page);await open(page);let mutations=0;const count=(r:any)=>{if(['POST','PUT','DELETE','PATCH'].includes(r.method()))mutations++};page.on('request',count)
 const before=snapshot();let modal=await review(page);await expect(modal.getByTestId('確認後工單進度-percent')).toHaveText('50%');await modal.getByRole('button',{name:'取消',exact:true}).click();expect(snapshot()).toEqual(before)
 modal=await review(page);await page.keyboard.press('Escape');await expect(modal).not.toBeVisible();expect(mutations).toBe(0);expect(snapshot()).toEqual(before);page.off('request',count);evidence.cancelMutationCounts.push(mutations)
 const capture=(r:any)=>{if(r.method()==='POST'&&r.url().endsWith('/confirm')){retryBody=r.postDataJSON();evidence.confirmationRequests++}};page.on('request',capture)
 posted=await confirm(page);page.off('request',capture)
 expect(posted.authorization_basis).toBe('LOCAL_DEMO_MANUAL');expect(posted.confirmation_id).toBe(retryBody.request_key)
 await expect(page.getByTestId('目前工單進度-good')).toContainText('1.25 ');await expect(page.getByTestId('目前工單進度-defective')).toContainText('0.25 ');await expect(page.getByTestId('目前工單進度-percent')).toHaveText('50%')
 await expect(page.getByRole('button',{name:'編輯回報',exact:true})).toHaveCount(0);await expect(page.getByRole('button',{name:'刪除回報',exact:true})).toHaveCount(0)
 const after=snapshot(),client=await playwright.request.newContext({baseURL:apiUrl,extraHTTPHeaders:{Connection:'close'}})
 const replay=await client.post(API_PATH+'/'+draft.id+'/confirm',{data:retryBody});expect(replay.status()).toBe(200);expect(snapshot()).toEqual(after)
 expect((await client.post(API_PATH+'/'+draft.id+'/confirm',{data:{...retryBody,row_version:999}})).status()).toBe(409)
 await client.dispose();expect(await existing()).toEqual(baseline);expect(await stock()).toBe('3');assertOldTablesUnchanged()
 await page.reload();await expect(page.getByTestId('comp-state')).toContainText('CONFIRMED');await expect(page.getByTestId('目前工單進度-percent')).toHaveText('50%')
 evidence.example={good:'1.25',defective:'0.25',total:'1.5',percent:'50',remaining:'1.25',stock:'3',workOrderState:'DRAFT',retry:200}
 await page.screenshot({path:path.join(artifacts,'completion-confirmed-desktop.png'),fullPage:true})
})

test('Desktop C: repeated partials, over-plan, no routing, adjacent errors, deletion and full real service restart',async({page,playwright})=>{
 watch(page);await open(page)
 await create(page,'COMP-PART-2','1.25','0.5');await confirm(page);await expect(page.getByTestId('目前工單進度-percent')).toHaveText('100%')
 await create(page,'COMP-OVER','0.125','0');await confirm(page);await expect(page.getByTestId('目前工單進度-percent')).toHaveText('105%');await expect(page.getByTestId('目前工單進度-remaining')).toContainText('-0.125')
 await expect(page.getByText('累計良品已超過計畫量；如實顯示超產與負的尚差量，未限制為 100%。',{exact:true})).toBeVisible()
 await create(page,'COMP-NO-ROUTING','0.5','0.25',1);await confirm(page)
 const invalid=await create(page,'COMP-INVALID','-0.5','0');await page.getByRole('button',{name:'檢視完工確認',exact:true}).click()
 const alert=page.getByRole('alert');await expect(alert).toContainText('不得為負數');expect(await alert.evaluate(e=>!!e.closest('.form-actions'))).toBe(true)
 page.once('dialog',d=>d.accept());await page.getByRole('button',{name:'刪除回報',exact:true}).click();await expect(page.getByRole('button',{name:'開啟完工回報 '+invalid.document_no,exact:true})).toHaveCount(0)
 persistDraft=await create(page,'COMP-PERSIST-DRAFT','-0.01','0')
 expect(await existing()).toEqual(baseline);assertOldTablesUnchanged()
 const p=await json(API_PATH+'/work-orders/'+fixtures.orders[0].id+'/progress');expect([p.confirmed_good_qty_base,p.confirmed_defective_qty_base,p.total_reported_qty_base,p.progress_percent,p.remaining_planned_good_qty_base]).toEqual(['2.625','0.75','3.375','105','-0.125'])
 const before=snapshot(),previous=ownership(runtime);await stop(runtime);runtime=await start();const restarted=ownership(runtime)
 expect(restarted.pid).not.toBe(previous.pid);expect(restarted.listeners.every((l:any)=>!previous.listeners.some((old:any)=>old.OwningProcess===l.OwningProcess))).toBe(true)
 expect(snapshot()).toEqual(before);expect(await existing()).toEqual(baseline);expect(await stock()).toBe('3')
 await open(page,persistDraft);await expect(page.getByTestId('comp-state')).toContainText('DRAFT');await expect(page.getByTestId('comp-good-value')).toContainText('-0.01')
 await open(page,draft);await page.reload();await expect(page.getByTestId('comp-state')).toContainText('CONFIRMED');await expect(page.getByTestId('目前工單進度-percent')).toHaveText('105%')
 const client=await playwright.request.newContext({baseURL:apiUrl,extraHTTPHeaders:{Connection:'close'}});expect((await client.post(API_PATH+'/'+draft.id+'/confirm',{data:retryBody})).status()).toBe(200);await client.dispose();expect(snapshot()).toEqual(before)
 evidence.restart={previous,restarted,allOldListenersStopped:true,persistedDraft:persistDraft.id,confirmedReport:draft.id,progress:p,stock:'3',exactOriginalRetry:200}
 await page.screenshot({path:path.join(artifacts,'completion-restart-desktop.png'),fullPage:true})
})
