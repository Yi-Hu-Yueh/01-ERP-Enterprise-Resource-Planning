import {test,expect,type Page} from '@playwright/test'
import {spawn,execFileSync,type ChildProcess} from 'node:child_process'
import {mkdirSync,createWriteStream,writeFileSync,readFileSync} from 'node:fs'
import path from 'node:path'
import {fileURLToPath} from 'node:url'
import {randomUUID} from 'node:crypto'

const root=path.resolve(fileURLToPath(new URL('../../',import.meta.url)))
const artifacts=path.resolve(root,process.env.ERP_TEST_ARTIFACTS||'artifacts/manual-round15b/browser/results'),task=path.dirname(artifacts)
mkdirSync(artifacts,{recursive:true})
const py=path.join(root,'.venv','Scripts','python.exe'),apiUrl='http://127.0.0.1:8024',uiUrl='http://127.0.0.1:5187'
const database=path.join(task,'local-demo-'+randomUUID()+'.sqlite')
type Runtime={child:ChildProcess;ports:number[];marker:string;name:string}
let runtime:Runtime,normal:Runtime|undefined,fixtures:any,posted:any,retryBody:any,other:any
const evidence:any={desktopOnly:true,mobileTests:0,ownerDatabaseUsed:false,pageErrors:[],stops:[],confirmationMutationCounts:[],postingRequestCount:0}
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
 if(!parent||String(parent.ExecutablePath).toLowerCase()!==py.toLowerCase()||!String(parent.CommandLine).includes(r.marker)||!String(parent.CommandLine).toLowerCase().includes(database.toLowerCase()))throw Error('QA runtime ownership mismatch; nothing stopped')
 const sockets=listeners(r.ports),ids=new Set(tree.map((p:any)=>p.ProcessId))
 if(sockets.length!==2||sockets.some((s:any)=>s.LocalAddress!=='127.0.0.1'||!ids.has(s.OwningProcess)))throw Error('QA listener ownership mismatch; nothing stopped')
 return {pid,tree,listeners:sockets,database,runtime:r.name}
}
async function start(demo=true){
 const ports=demo?[8024,5187]:[8025,5188],name=demo?'demo':'ordinary',marker=demo?'backend.local_demo':'scripts/dev.py'
 expect(listeners(ports)).toEqual([])
 const args=demo?['-u','-m','backend.local_demo','--database',database,'--backend-port',String(ports[0]),'--frontend-port',String(ports[1])]:['-u','scripts/dev.py','--database',database,'--backend-port',String(ports[0]),'--frontend-port',String(ports[1])]
 const child=spawn(py,args,{cwd:root,windowsHide:true,stdio:['ignore','pipe','pipe']})
 child.stdout?.pipe(createWriteStream(path.join(artifacts,name+'-launcher.log'),{flags:'a'}));child.stderr?.pipe(createWriteStream(path.join(artifacts,name+'-launcher-error.log'),{flags:'a'}))
 const r={child,ports,marker,name}
 await ready('http://127.0.0.1:'+ports[0]+'/api/health');await ready('http://127.0.0.1:'+ports[1])
 return r
}
async function stop(r:Runtime){
 const before=ownership(r)
 execFileSync('taskkill.exe',['/PID',String(r.child.pid),'/T','/F'],{windowsHide:true,stdio:'pipe'})
 await new Promise<void>(resolve=>{if(r.child.exitCode!==null||r.child.signalCode!==null)resolve();else r.child.once('exit',()=>resolve())})
 await offline('http://127.0.0.1:'+r.ports[0]+'/api/health');await offline('http://127.0.0.1:'+r.ports[1]);expect(listeners(r.ports)).toEqual([])
 evidence.stops.push({before,afterListeners:[]})
}
function snapshot(){return JSON.parse(execFileSync(py,['-c','import json,sys;from backend.tests.test_supplier_offers import snapshot;print(json.dumps(snapshot(sys.argv[1])))',database],{cwd:root,windowsHide:true,encoding:'utf8',stdio:['ignore','pipe','pipe']}))}
async function json(url:string){const r=await fetch(apiUrl+url);expect(r.status).toBe(200);return r.json()}
async function stock(){return (await json('/api/stock-positions')).find((p:any)=>p.item_id===fixtures.raws[0].id&&p.lot_code==='LOT-R8-001').on_hand_qty_base}
function watch(page:Page){page.on('pageerror',e=>evidence.pageErrors.push(e.message))}
async function open(page:Page,id=fixtures.issue.id){
 const number=id===fixtures.issue.id?'ISSUE-DEMO-001':other.document_no
 const row=page.getByRole('button',{name:'開啟領料單 '+number,exact:true})
 if(page.url().startsWith(uiUrl+'/#material-issues')&&await row.count())await row.click()
 else await page.goto('/#material-issues?record='+id)
 await expect(page.getByTestId('mi-number-value')).toHaveText(number)
}
async function review(page:Page){
 await page.getByRole('button',{name:'確認領料過帳',exact:true}).click()
 const dialog=page.getByRole('dialog',{name:'確認領料過帳',exact:true});await expect(dialog).toBeVisible();return dialog
}

test.beforeAll(async()=>{
 expect(listeners([8024,5187,8025,5188])).toEqual([])
 const output=execFileSync(py,['-c','import json,sys;from pathlib import Path;from backend.tests.test_local_demo_issues import seed_demo_database;print(json.dumps(seed_demo_database(Path(sys.argv[1])),ensure_ascii=False))',database],{cwd:root,windowsHide:true,encoding:'utf8',env:{...process.env,PYTHONIOENCODING:'utf-8'},stdio:['ignore','pipe','pipe']})
 fixtures=JSON.parse(output);writeFileSync(path.join(artifacts,'fixture-description.json'),JSON.stringify(fixtures,null,2))
 runtime=await start();evidence.initialOwnership=ownership(runtime)
})
test.afterEach(()=>expect(evidence.pageErrors).toEqual([]))
test.afterAll(async()=>{
 try{
  if(normal&&normal.child.exitCode===null&&normal.child.signalCode===null)await stop(normal)
  if(runtime&&runtime.child.exitCode===null&&runtime.child.signalCode===null)await stop(runtime)
 }finally{writeFileSync(path.join(artifacts,'local-demo-desktop-evidence.json'),JSON.stringify(evidence,null,2))}
})

test('Desktop reviewed local-demo confirmation cancels without mutation and leaves stock4',async({page})=>{
 watch(page);await open(page)
 await expect(page.getByText('本機展示模式／尚未啟用正式電子簽核與身分權限驗證',{exact:true})).toBeVisible()
 await expect(page.getByTestId('mi-state')).toContainText('草稿')
 const before=snapshot();let mutations=0
 const count=(r:any)=>{if(['POST','PUT','DELETE','PATCH'].includes(r.method()))mutations++}
 page.on('request',count)
 const dialog=await review(page)
 for(const text of ['ISSUE-DEMO-001',fixtures.orders[0].document_no,fixtures.raws[0].code,fixtures.warehouse.code,'LOT-R8-001','1.75','-1.75','4'])await expect(dialog).toContainText(text)
 const dialogBox=await dialog.boundingBox();expect(dialogBox).not.toBeNull();expect(dialogBox!.width).toBeGreaterThan(800)
 await dialog.screenshot({path:path.join(artifacts,'desktop-local-demo-confirmation.png')})
 await expect(dialog.getByRole('button',{name:'取消',exact:true})).toBeFocused()
 expect(await stock()).toBe('4');expect(snapshot()).toEqual(before)
 await dialog.getByRole('button',{name:'取消',exact:true}).click();await expect(dialog).toHaveCount(0)
 await expect(page.getByRole('button',{name:'確認領料過帳',exact:true})).toBeFocused()
 await review(page);await page.keyboard.press('Escape');await expect(page.getByRole('dialog')).toHaveCount(0)
 await page.reload();await expect(page.getByTestId('mi-state')).toContainText('草稿')
 await page.getByRole('button',{name:'重新讀取工單與庫存參考',exact:true}).click()
 await expect(page.getByRole('status').filter({hasText:'參考資料與實體庫存已更新'})).toBeVisible()
 page.off('request',count);expect(mutations).toBe(0);expect(snapshot()).toEqual(before);expect(await stock()).toBe('4')
 evidence.confirmationMutationCounts.push(mutations);evidence.reviewAndCancel={stock:'4',draft:true,f5:true,escape:true,sourceReload:true}
})

test('Desktop final human confirmation uses authoritative engine once, exact2.25 stock and preliminary MRP9.3125',async({page,request})=>{
 watch(page);await open(page)
 const original=fixtures.issue,header=await request.post(apiUrl+'/api/material-issues',{data:{request_key:randomUUID(),document_no:'ISSUE-DEMO-RESTART-DRAFT',work_order_id:original.work_order_id,work_order_version:original.work_order_version,business_date:'2026-11-20',note:'Unposted restart control'}})
 expect(header.status()).toBe(201);other=await header.json()
 const source=original.lines[0],line=await request.post(apiUrl+'/api/material-issues/'+other.id+'/lines',{data:{request_key:randomUUID(),document_version:other.row_version,material_id:source.material_id,warehouse_id:source.warehouse_id,position_id:source.position_id,quantity:'0.25',note:null,bom_component_id:null,bom_component_version:null}})
 expect(line.status()).toBe(201)
 const dialog=await review(page);expect(await stock()).toBe('4')
 let posts=0
 page.on('request',r=>{if(r.method()==='POST'&&r.url().endsWith('/post'))posts++})
 await page.route('**/api/material-issues/'+original.id+'/post',async route=>{const response=await route.fetch();await new Promise(resolve=>setTimeout(resolve,200));await route.fulfill({response})})
 const response=page.waitForResponse(r=>r.request().method()==='POST'&&r.url().endsWith('/post'))
 await dialog.getByRole('button',{name:'確認並扣庫存',exact:true}).dblclick({force:true})
 const result=await response;expect(result.status()).toBe(201);posted=await result.json();retryBody=result.request().postDataJSON()
 await expect(dialog).toHaveCount(0);await expect(page.getByTestId('mi-state')).toContainText('實際庫存已扣除')
 await expect(page.getByRole('status').filter({hasText:'領料已正式過帳'})).toBeVisible()
 expect(posts).toBe(1);evidence.postingRequestCount=posts
 expect(posted.authorization_basis).toBe('LOCAL_DEMO_MANUAL');expect(posted.work_order.approval_state).toBe('DRAFT')
 expect(posted.movements).toHaveLength(1);expect(posted.movements[0].qty_base_delta).toBe('-1.75');expect(await stock()).toBe('2.25')
 const before=snapshot()
 for(let i=0;i<2;i++)expect((await request.post(apiUrl+'/api/material-issues/'+original.id+'/post',{data:retryBody})).status()).toBe(200)
 expect((await request.post(apiUrl+'/api/material-issues/'+original.id+'/post',{data:{...retryBody,row_version:retryBody.row_version+1}})).status()).toBe(409)
 expect(snapshot()).toEqual(before)
 const mrp=await request.post(apiUrl+'/api/mrp/preview',{data:{work_order_ids:fixtures.orders.map((w:any)=>w.id),warehouse_ids:[fixtures.warehouse.id]}})
 expect(mrp.status()).toBe(200);const data=await mrp.json(),material=data.materials.find((m:any)=>m.material.id===fixtures.raws[0].id)
 expect([material.gross_demand_qty_base,material.physical_stock_qty_base,material.shortage_qty_base]).toEqual(['11.5625','2.25','9.3125']);expect(data.preliminary).toBe(true)
 await expect(page.getByRole('button',{name:'編輯領料表頭',exact:true})).toHaveCount(0);await expect(page.getByRole('button',{name:/編輯領料明細/})).toHaveCount(0)
 await page.reload();await expect(page.getByTestId('mi-state')).toContainText('已過帳');expect(await stock()).toBe('2.25')
 await page.screenshot({path:path.join(artifacts,'desktop-local-demo-posted.png'),fullPage:true})
 evidence.posting={before:'4',afterDraft:'4',afterConfirm:'2.25',movement:'-1.75',postingId:posted.posting_id,postingCount:1,afterRetries:'2.25',f5:'2.25',mrpGross:'11.5625',mrpPhysical:'2.25',mrpPreliminaryDifference:'9.3125',workOrderState:'DRAFT'}
})

test('Desktop true local-demo restart persists exact history and ordinary runtime still disables new posting',async({page,request})=>{
 watch(page);const before=snapshot(),old=ownership(runtime);await stop(runtime);runtime=await start();const fresh=ownership(runtime)
 expect(fresh.pid).not.toBe(old.pid);expect(fresh.listeners.every((s:any)=>!old.listeners.some((p:any)=>p.OwningProcess===s.OwningProcess))).toBe(true)
 const policy=await json('/api/material-issues/policy');expect(policy.authorization_basis).toBe('LOCAL_DEMO_MANUAL');expect(policy.posting_allowed).toBe(true)
 await open(page);await expect(page.getByTestId('mi-state')).toContainText('已過帳');expect(await stock()).toBe('2.25')
 expect((await request.post(apiUrl+'/api/material-issues/'+fixtures.issue.id+'/post',{data:retryBody})).status()).toBe(200)
 await open(page,other.id);await expect(page.getByTestId('mi-state')).toContainText('草稿');await expect(page.getByRole('button',{name:'確認領料過帳',exact:true})).toBeEnabled()
 expect(snapshot()).toEqual(before)
 normal=await start(false);evidence.ordinaryOwnership=ownership(normal)
 await page.goto('http://127.0.0.1:5188/#material-issues?record='+other.id)
 await expect(page.getByTestId('mi-state')).toContainText('草稿');await expect(page.getByRole('button',{name:'正式領料過帳（政策待核定）',exact:true})).toBeDisabled()
 await expect(page.getByRole('button',{name:'確認領料過帳',exact:true})).toHaveCount(0)
 const ordinaryPolicy=await request.get('http://127.0.0.1:8025/api/material-issues/policy');expect((await ordinaryPolicy.json()).posting_allowed).toBe(false)
 const pending=await json('/api/material-issues/'+other.id)
 const denied=await request.post('http://127.0.0.1:8025/api/material-issues/'+other.id+'/post?authorized=true',{headers:{'X-Local-Demo':'true'},data:{request_key:randomUUID(),row_version:pending.row_version,work_order_version:pending.work_order_version}})
 expect(denied.status()).toBe(403);expect(snapshot()).toEqual(before);expect(await stock()).toBe('2.25')
 evidence.restart={old,fresh,zeroListenersObserved:true,demoAuthorizationAfterExplicitRestart:true,postedImmutable:true,draftPersisted:true,ordinaryPostingStatus:403,ordinaryUiDisabled:true,finalStock:'2.25'}
})
