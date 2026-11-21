import {test,expect,type Page} from '@playwright/test'
import {spawn,execFileSync,type ChildProcess} from 'node:child_process'
import {mkdirSync,createWriteStream,writeFileSync} from 'node:fs'
import path from 'node:path'
import {fileURLToPath} from 'node:url'
import {randomUUID} from 'node:crypto'

const root=path.resolve(fileURLToPath(new URL('../../',import.meta.url)))
const artifacts=path.resolve(root,process.env.ERP_TEST_ARTIFACTS||'artifacts/manual-round16/browser/results'),task=path.dirname(artifacts)
mkdirSync(artifacts,{recursive:true})
const py=path.join(root,'.venv','Scripts','python.exe'),apiUrl='http://127.0.0.1:8026',uiUrl='http://127.0.0.1:5189'
const database=path.join(task,'material-return-'+randomUUID()+'.sqlite')
type Runtime={child:ChildProcess;marker:string;name:string}
let runtime:Runtime,fixtures:any,draft:any,retryBody:any,posted:any
const evidence:any={desktopOnly:true,mobileTests:0,ownerDatabaseUsed:false,pageErrors:[],stops:[],cancelMutationCounts:[],postingRequests:0}
test.use({baseURL:uiUrl,viewport:{width:1440,height:1100},isMobile:false,screenshot:'off',trace:'off'})
test.describe.configure({mode:'serial'})

function ps(script:string){return JSON.parse(execFileSync('powershell.exe',['-NoProfile','-Command',script],{windowsHide:true,encoding:'utf8',stdio:['ignore','pipe','pipe']}))}
function listeners(){return ps('ConvertTo-Json -InputObject @(Get-NetTCPConnection -State Listen | Where-Object LocalPort -in 8026,5189 | Select-Object LocalAddress,LocalPort,OwningProcess) -Compress')}
async function ready(url:string){await expect.poll(async()=>{try{return(await fetch(url,{signal:AbortSignal.timeout(1000)})).status}catch{return 0}},{timeout:45_000}).toBe(200)}
async function offline(url:string){await expect.poll(async()=>{try{return(await fetch(url,{signal:AbortSignal.timeout(1000)})).status}catch{return 0}},{timeout:20_000}).toBe(0)}
function ownership(r:Runtime){
 const pid=Number(r.child.pid)
 const tree=ps('$qaProcesses=Get-CimInstance Win32_Process; $qaIds=@('+pid+'); do {$qaChildren=@($qaProcesses | Where-Object {$_.ParentProcessId -in $qaIds -and $_.ProcessId -notin $qaIds} | Select-Object -ExpandProperty ProcessId); $qaIds+=$qaChildren}while($qaChildren.Count -gt 0); ConvertTo-Json -InputObject @($qaProcesses | Where-Object ProcessId -in $qaIds | Select-Object ProcessId,ParentProcessId,ExecutablePath,CommandLine) -Depth 5 -Compress')
 const parent=tree.find((p:any)=>p.ProcessId===pid)
 if(!parent||String(parent.ExecutablePath).toLowerCase()!==py.toLowerCase()||!String(parent.CommandLine).includes(r.marker)||!String(parent.CommandLine).toLowerCase().includes(database.toLowerCase()))throw Error('Return QA launcher ownership mismatch; nothing stopped')
 const sockets=listeners(),ids=new Set(tree.map((p:any)=>p.ProcessId))
 if(sockets.length!==2||sockets.some((s:any)=>s.LocalAddress!=='127.0.0.1'||!ids.has(s.OwningProcess)))throw Error('Return QA listener ownership mismatch; nothing stopped')
 return {pid,tree,listeners:sockets,database,runtime:r.name}
}
async function start(controlled=false){
 expect(listeners()).toEqual([])
 const script=[
  'import os,subprocess,sys,shutil,uvicorn',
  'from pathlib import Path',
  'from backend.app.db import ROOT',
  'from backend.tests.material_return_qa_runtime import create_material_return_qa_app',
  'from scripts.dev import stop',
  'app=create_material_return_qa_app(Path(sys.argv[1]),backend_port=8026,frontend_port=5189)',
  'frontend=subprocess.Popen([shutil.which("node"),str(ROOT/"frontend/node_modules/vite/bin/vite.js"),"--host","127.0.0.1","--port","5189","--strictPort"],cwd=ROOT/"frontend",env={**os.environ,"ERP_API_TARGET":"http://127.0.0.1:8026"},creationflags=subprocess.CREATE_NO_WINDOW if os.name=="nt" else 0)',
  'try: uvicorn.run(app,host="127.0.0.1",port=8026,proxy_headers=False)',
  'finally: stop(frontend)',
 ].join('\n')
 const args=controlled?['-u','-c',script,database]:['-u','scripts/dev.py','--database',database,'--backend-port','8026','--frontend-port','5189']
 const name=controlled?'return-qa':'ordinary-return-closed',marker=controlled?'create_material_return_qa_app':'scripts/dev.py'
 const child=spawn(py,args,{cwd:root,windowsHide:true,stdio:['ignore','pipe','pipe']})
 child.stdout?.pipe(createWriteStream(path.join(artifacts,name+'-launcher.log'),{flags:'a'}));child.stderr?.pipe(createWriteStream(path.join(artifacts,name+'-launcher-error.log'),{flags:'a'}))
 const r={child,marker,name};await ready(apiUrl+'/api/health');await ready(uiUrl);return r
}
async function stop(r:Runtime){
 const before=ownership(r)
 execFileSync('taskkill.exe',['/PID',String(r.child.pid),'/T','/F'],{windowsHide:true,stdio:'pipe'})
 await new Promise<void>(resolve=>{if(r.child.exitCode!==null||r.child.signalCode!==null)resolve();else r.child.once('exit',()=>resolve())})
 await offline(apiUrl+'/api/health');await offline(uiUrl);expect(listeners()).toEqual([]);evidence.stops.push({before,afterListeners:[]})
}
function snapshot(){return JSON.parse(execFileSync(py,['-c','import json,sys;from backend.tests.test_supplier_offers import snapshot;print(json.dumps(snapshot(sys.argv[1])))',database],{cwd:root,windowsHide:true,encoding:'utf8',stdio:['ignore','pipe','pipe']}))}
async function json(url:string){const r=await fetch(apiUrl+url);expect(r.status).toBe(200);return r.json()}
async function stock(){return (await json('/api/stock-positions')).find((p:any)=>p.item_id===fixtures.raws[0].id&&p.lot_code==='LOT-R8-001').on_hand_qty_base}
function watch(page:Page){page.on('pageerror',e=>evidence.pageErrors.push(e.message))}
async function open(page:Page,row=draft){
 const button=page.getByRole('button',{name:'開啟退料單 '+row.document_no,exact:true})
 if(page.url().startsWith(uiUrl+'/#material-returns')&&await button.count())await button.click()
 else await page.goto('/#material-returns?record='+row.id)
 await expect(page.getByTestId('mr-number-value')).toHaveText(row.document_no)
}
async function create(page:Page,number:string){
 await page.getByRole('button',{name:'＋ 新增退料單',exact:true}).click()
 await page.getByLabel('退料單號 *',{exact:true}).fill(number)
 await page.getByLabel('製造工單 *',{exact:true}).selectOption(fixtures.orders[0].id)
 await page.getByLabel('退料日期 *',{exact:true}).fill('2026-11-20')
 await page.getByLabel('備註',{exact:true}).fill('Desktop isolated QA')
 const response=page.waitForResponse(r=>r.url().endsWith('/api/material-returns')&&r.request().method()==='POST')
 await page.getByRole('button',{name:'儲存退料草稿',exact:true}).click()
 const result=await response;expect(result.status()).toBe(201);const row=await result.json()
 await expect(page.getByTestId('mr-number-value')).toHaveText(number);return row
}
async function add(page:Page,quantity:string){
 await page.getByRole('button',{name:'＋ 新增退料明細',exact:true}).click()
 await page.getByLabel('來源已過帳領料單 *',{exact:true}).selectOption(fixtures.issue.id)
 await page.getByLabel('原領料明細 *',{exact:true}).selectOption(fixtures.issue.lines[0].id)
 await page.getByLabel('本次退料量 *',{exact:true}).fill(quantity)
 await page.getByRole('button',{name:'儲存退料明細',exact:true}).click()
 await expect(page.getByRole('form',{name:'新增退料明細',exact:true})).toHaveCount(0)
}
async function review(page:Page){await page.getByRole('button',{name:'檢視退料過帳',exact:true}).click();const dialog=page.getByRole('dialog',{name:'檢視退料過帳',exact:true});await expect(dialog).toBeVisible();return dialog}

test.beforeAll(async()=>{
 expect(listeners()).toEqual([])
 const output=execFileSync(py,['-c','import json,sys;from pathlib import Path;from backend.tests.material_return_qa_runtime import seed_material_return_qa_database;print(json.dumps(seed_material_return_qa_database(Path(sys.argv[1])),ensure_ascii=False))',database],{cwd:root,windowsHide:true,encoding:'utf8',env:{...process.env,PYTHONIOENCODING:'utf-8'},stdio:['ignore','pipe','pipe']})
 fixtures=JSON.parse(output);writeFileSync(path.join(artifacts,'fixture-description.json'),JSON.stringify(fixtures,null,2));runtime=await start();evidence.initialOwnership=ownership(runtime)
})
test.afterEach(()=>expect(evidence.pageErrors).toEqual([]))
test.afterAll(async()=>{try{if(runtime&&runtime.child.exitCode===null&&runtime.child.signalCode===null)await stop(runtime)}finally{writeFileSync(path.join(artifacts,'material-return-desktop-evidence.json'),JSON.stringify(evidence,null,2))}})

test('Desktop draft header and single expandable detail CRUD, zero-write Cancel, search and F5',async({page})=>{
 watch(page);await page.goto('/#material-returns');await page.getByRole('button',{name:'＋ 新增退料單',exact:true}).waitFor()
 let mutations=0;const count=(r:any)=>{if(['POST','PUT','DELETE','PATCH'].includes(r.method()))mutations++}
 page.on('request',count);const initial=snapshot()
 await page.getByRole('button',{name:'＋ 新增退料單',exact:true}).click();await page.getByLabel('退料單號 *',{exact:true}).fill('UNSAVED-HEADER')
 await page.getByRole('button',{name:'取消',exact:true}).click();expect(mutations).toBe(0);expect(snapshot()).toEqual(initial)
 page.off('request',count);draft=await create(page,'MRET-TEST-001');expect(await stock()).toBe('2.25')
 await add(page,'0.75');await expect(page.getByTestId('mr-line-1')).toContainText('可退 1.75');expect(await stock()).toBe('2.25')
 await page.getByRole('button',{name:'展開編輯退料明細 1',exact:true}).click()
 await expect(page.getByRole('form',{name:'編輯退料明細',exact:true})).toHaveCount(1)
 await expect(page.getByRole('button',{name:'＋ 新增退料明細',exact:true})).toBeDisabled()
 await page.getByLabel('本次退料量 *',{exact:true}).fill('9.99');const beforeCancel=snapshot();mutations=0;page.on('request',count)
 await page.getByRole('button',{name:'取消',exact:true}).click();expect(mutations).toBe(0);expect(snapshot()).toEqual(beforeCancel);page.off('request',count)
 await expect(page.getByTestId('mr-line-1')).toContainText('本次 0.75')
 await page.getByRole('button',{name:'展開編輯退料明細 1',exact:true}).click();await page.getByLabel('明細備註',{exact:true}).fill('Saved one line at a time')
 await page.getByRole('button',{name:'儲存退料明細',exact:true}).click();await expect(page.getByTestId('mr-line-1')).toContainText('Saved one line at a time')
 await add(page,'0.1');page.once('dialog',d=>d.accept());await page.getByRole('button',{name:'刪除退料明細 2',exact:true}).click();await expect(page.getByTestId('mr-line-2')).toHaveCount(0)
 await page.getByRole('button',{name:'編輯退料表頭',exact:true}).click();await page.getByLabel('備註',{exact:true}).fill('Saved header edit');await page.getByRole('button',{name:'儲存退料表頭變更',exact:true}).click()
 await expect(page.getByRole('button',{name:'編輯退料表頭',exact:true})).toBeVisible()
 await page.getByRole('button',{name:'編輯退料表頭',exact:true}).click();await page.getByLabel('備註',{exact:true}).fill('UNSAVED');const beforeHeader=snapshot();mutations=0;page.on('request',count)
 await page.getByRole('button',{name:'取消',exact:true}).click();expect(mutations).toBe(0);expect(snapshot()).toEqual(beforeHeader);page.off('request',count)
 const extra=await create(page,'MRET-DELETE-CONTROL');page.once('dialog',d=>d.accept());await page.getByRole('button',{name:'刪除退料草稿',exact:true}).click();await expect(page.getByRole('button',{name:'開啟退料單 '+extra.document_no,exact:true})).toHaveCount(0)
 await page.getByLabel('搜尋退料單或工單',{exact:true}).fill('MRET-TEST-001');await page.getByRole('button',{name:'搜尋',exact:true}).click();await open(page)
 await page.reload();await expect(page.getByTestId('mr-number-value')).toHaveText('MRET-TEST-001');await expect(page.getByTestId('mr-line-1')).toContainText('本次 0.75');await expect(page.getByTestId('mr-state')).toContainText('草稿')
 expect(await stock()).toBe('2.25');await page.screenshot({path:path.join(artifacts,'desktop-return-draft.png'),fullPage:true})
 evidence.cancelMutationCounts.push(0,0,0);evidence.draft={headerCRUD:true,lineCRUD:true,oneEditor:true,search:true,f5:true,stock:'2.25'}
})

test('Desktop ordinary runtime leaves return gate CLOSED while review and Cancel/Escape/reload stay read-only',async({page})=>{
 watch(page);await open(page)
 expect((await json('/api/material-issues/policy')).posting_allowed).toBe(false)
 expect((await json('/api/material-returns/policy')).posting_allowed).toBe(false)
 await expect(page.getByTestId('mr-policy')).toContainText('CLOSED')
 const before=snapshot();let mutations=0;const count=(r:any)=>{if(['POST','PUT','DELETE','PATCH'].includes(r.method()))mutations++};page.on('request',count)
 const dialog=await review(page)
 for(const value of ['MRET-TEST-001','ISSUE-TEST-001',fixtures.raws[0].code,fixtures.warehouse.code,'LOT-R8-001','原領 1.75','已退 0','可退 1.75','本行 0.75','+0.75','2.25','3','本單後可退 1'])await expect(dialog).toContainText(value)
 await expect(dialog.getByText('本機展示模式／尚未啟用正式電子簽核與身分權限驗證',{exact:true})).toBeVisible()
 await expect(dialog.getByRole('button',{name:'確認並增加庫存',exact:true})).toBeDisabled();await expect(dialog.getByRole('button',{name:'取消',exact:true})).toBeFocused()
 const box=await dialog.boundingBox();expect(box?.width).toBeGreaterThan(1000)
 await dialog.screenshot({path:path.join(artifacts,'desktop-return-closed-review.png')})
 await dialog.getByRole('button',{name:'取消',exact:true}).click();await expect(dialog).toHaveCount(0)
 await review(page);await page.keyboard.press('Escape');await expect(page.getByRole('dialog')).toHaveCount(0)
 await page.getByRole('button',{name:'重新讀取領料來源與庫存',exact:true}).click();await expect(page.getByRole('status').filter({hasText:'已重新讀取已過帳來源'})).toBeVisible()
 page.off('request',count);expect(mutations).toBe(0);expect(snapshot()).toEqual(before);expect(await stock()).toBe('2.25')
 evidence.cancelMutationCounts.push(mutations);evidence.closedGate={ordinaryIssueGrant:false,returnGrant:false,reviewReadOnly:true,confirmDisabled:true,escape:true,sourceReload:true,stock:'2.25'}
})

test('Desktop real QA +0.75 posting, duplicate-click lock and exact uncertain-result retry restore stock once',async({page,request})=>{
 watch(page);await stop(runtime);runtime=await start(true);evidence.controlledOwnership=ownership(runtime)
 await open(page);expect((await json('/api/material-returns/policy')).posting_allowed).toBe(true)
 const dialog=await review(page);await expect(dialog.getByRole('button',{name:'確認並增加庫存',exact:true})).toBeEnabled();expect(await stock()).toBe('2.25')
 const bodies:any[]=[];let first=true
 await page.route('**/api/material-returns/'+draft.id+'/post',async route=>{
  bodies.push(route.request().postDataJSON());const response=await route.fetch()
  if(first){first=false;expect(response.status()).toBe(201);await new Promise(resolve=>setTimeout(resolve,250));await route.abort('failed')}
  else{expect(response.status()).toBe(200);await route.fulfill({response})}
 })
 await dialog.getByRole('button',{name:'確認並增加庫存',exact:true}).dblclick({force:true})
 await expect(dialog.getByRole('alert')).toContainText('保留原請求重試');expect(bodies).toHaveLength(1);expect(await stock()).toBe('3')
 await dialog.getByRole('button',{name:'取消',exact:true}).click();await expect(page.getByRole('status').filter({hasText:'上次請求可能已提交'})).toBeVisible()
 const reopened=await review(page);await expect(reopened.getByRole('button',{name:'原請求重試並核對結果',exact:true})).toBeEnabled()
 await reopened.getByRole('button',{name:'原請求重試並核對結果',exact:true}).click();await expect(reopened).toHaveCount(0)
 expect(bodies).toHaveLength(2);expect(bodies[1]).toEqual(bodies[0]);retryBody=bodies[0]
 posted=await json('/api/material-returns/'+draft.id);expect(posted.posting_state).toBe('POSTED');expect(posted.movements).toHaveLength(1);expect(posted.movements[0].qty_base_delta).toBe('0.75')
 const line=posted.lines[0];expect([line.source.issued_qty_base,line.source.returned_qty_base,line.source.returnable_qty_base,line.proposed_qty_base,line.remaining_after_proposal_qty_base]).toEqual(['1.75','0.75','1','0.75','1'])
 expect(await stock()).toBe('3');await expect(page.getByTestId('mr-state')).toContainText('實際庫存已增加');await expect(page.getByTestId('mr-movements')).toContainText('+0.75')
 await expect(page.getByRole('button',{name:'編輯退料表頭',exact:true})).toHaveCount(0);await expect(page.getByRole('button',{name:/展開編輯退料明細/})).toHaveCount(0)
 const before=snapshot();expect((await request.post(apiUrl+'/api/material-returns/'+draft.id+'/post',{data:retryBody})).status()).toBe(200)
 expect((await request.post(apiUrl+'/api/material-returns/'+draft.id+'/post',{data:{...retryBody,row_version:retryBody.row_version+1}})).status()).toBe(409);expect(snapshot()).toEqual(before)
 await page.reload();await expect(page.getByTestId('mr-state')).toContainText('已過帳');expect(await stock()).toBe('3')
 await page.screenshot({path:path.join(artifacts,'desktop-return-posted.png'),fullPage:true})
 evidence.postingRequests=bodies.length;evidence.posting={draftStock:'2.25',movement:'+0.75',physical:'3',originalIssued:'1.75',returned:'0.75',returnable:'1',oneMovement:true,duplicateClickRequests:1,uncertainCommittedResponse:true,exactOriginalRetry:true,conflictStatus:409,f5:true}
})

test('Desktop actual full shutdown/restart retains immutable history, overreturn rejection and shared stock readers',async({page,request})=>{
 watch(page);const before=snapshot(),old=ownership(runtime);await stop(runtime);runtime=await start(true);const fresh=ownership(runtime)
 expect(fresh.pid).not.toBe(old.pid);expect(fresh.listeners.every((s:any)=>!old.listeners.some((p:any)=>p.OwningProcess===s.OwningProcess))).toBe(true)
 await open(page);await expect(page.getByTestId('mr-state')).toContainText('已過帳');expect(await stock()).toBe('3')
 expect((await request.post(apiUrl+'/api/material-returns/'+draft.id+'/post',{data:retryBody})).status()).toBe(200);expect(snapshot()).toEqual(before)
 const issue=await json('/api/material-issues/'+fixtures.issue.id);expect(issue.posting_state).toBe('POSTED');expect(issue.movements).toHaveLength(1);expect(issue.movements[0].qty_base_delta).toBe('-1.75');expect(issue.lines[0].position.physical_qty_base).toBe('3')
 const source=(await json('/api/material-returns/sources')).find((s:any)=>s.origin_line_id===fixtures.issue.lines[0].id);expect([source.issued_qty_base,source.returned_qty_base,source.returnable_qty_base]).toEqual(['1.75','0.75','1'])
 const supplierSources=await json('/api/supplier-returns/source-receipts');const supplierLines=supplierSources.flatMap((r:any)=>r.lines).filter((l:any)=>l.item_id===fixtures.raws[0].id&&l.lot_code==='LOT-R8-001');expect(supplierLines.length).toBeGreaterThan(0);for(const line of supplierLines)expect(line.on_hand_qty_base).toBe('3')
 const mrp=await request.post(apiUrl+'/api/mrp/preview',{data:{work_order_ids:fixtures.orders.map((w:any)=>w.id),warehouse_ids:[fixtures.warehouse.id]}});expect(mrp.status()).toBe(200)
 const preview=await mrp.json(),material=preview.materials.find((m:any)=>m.material.id===fixtures.raws[0].id);expect([material.gross_demand_qty_base,material.physical_stock_qty_base,material.shortage_qty_base]).toEqual(['11.5625','3','8.5625']);expect(preview.preliminary).toBe(true)
 const over=await create(page,'MRET-OVERRETURN-QA');await add(page,'2');const overBefore=snapshot();await page.getByRole('button',{name:'檢視退料過帳',exact:true}).click();await expect(page.getByRole('alert')).toBeVisible();await expect(page.getByRole('dialog')).toHaveCount(0);expect(snapshot()).toEqual(overBefore);expect(await stock()).toBe('3')
 await page.screenshot({path:path.join(artifacts,'desktop-return-overreturn-feedback.png'),fullPage:true})
 await page.reload();await expect(page.getByTestId('mr-number-value')).toHaveText(over.document_no);await expect(page.getByTestId('mr-state')).toContainText('草稿');await open(page)
 evidence.restart={old,fresh,zeroListenersObserved:true,durablePosted:true,durableOriginalRetry:true,durableDraft:true,finalStock:'3'};evidence.readers={issue:'-1.75',issueCurrentStock:'3',supplierSourceStock:'3',mrpGross:'11.5625',mrpPhysical:'3',preliminaryDifference:'8.5625',notRemainingProcurement:true,overreturnReadOnlyRejected:true}
})
