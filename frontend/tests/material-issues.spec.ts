import {test,expect,type Page} from '@playwright/test'
import {spawn,execFileSync,type ChildProcess} from 'node:child_process'
import {mkdirSync,createWriteStream,writeFileSync,readFileSync} from 'node:fs'
import path from 'node:path'
import {fileURLToPath} from 'node:url'
import {randomUUID} from 'node:crypto'
const root=path.resolve(fileURLToPath(new URL('../../',import.meta.url)))
const artifacts=path.resolve(root,process.env.ERP_TEST_ARTIFACTS||'artifacts/manual-round15/browser/results'),task=path.dirname(artifacts)
mkdirSync(artifacts,{recursive:true})
const py=path.join(root,'.venv','Scripts','python.exe'),apiUrl='http://127.0.0.1:8023',uiUrl='http://127.0.0.1:5186'
let database=path.join(task,'normal-issues-'+randomUUID()+'.sqlite'),launcher:ChildProcess,fixtures:any,draft:any,authorized=false
const evidence:any={desktopOnly:true,mobileTests:0,pageErrors:[],stops:[],cancelMutations:[],ownerDatabaseUsed:false}
test.use({baseURL:uiUrl,viewport:{width:1440,height:1100},isMobile:false,screenshot:'off',trace:'off'})
test.describe.configure({mode:'serial'})
function ps(script:string){return JSON.parse(execFileSync('powershell.exe',['-NoProfile','-Command',script],{windowsHide:true,encoding:'utf8',stdio:['ignore','pipe','pipe']}))}
function listeners(){return ps('ConvertTo-Json -InputObject @(Get-NetTCPConnection -State Listen | Where-Object LocalPort -in 8023,5186 | Select-Object LocalAddress,LocalPort,OwningProcess) -Compress')}
async function ready(url:string){await expect.poll(async()=>{try{return(await fetch(url,{signal:AbortSignal.timeout(1000)})).status}catch{return 0}},{timeout:45_000}).toBe(200)}
async function offline(url:string){await expect.poll(async()=>{try{return(await fetch(url,{signal:AbortSignal.timeout(1000)})).status}catch{return 0}},{timeout:20_000}).toBe(0)}
function ownership(){
 const pid=Number(launcher.pid)
 const tree=ps('$qaProcesses=Get-CimInstance Win32_Process; $qaIds=@('+pid+'); do {$qaChildren=@($qaProcesses | Where-Object {$_.ParentProcessId -in $qaIds -and $_.ProcessId -notin $qaIds} | Select-Object -ExpandProperty ProcessId); $qaIds+=$qaChildren}while($qaChildren.Count -gt 0); ConvertTo-Json -InputObject @($qaProcesses | Where-Object ProcessId -in $qaIds | Select-Object ProcessId,ParentProcessId,ExecutablePath,CommandLine) -Depth 5 -Compress')
 const parent=tree.find((p:any)=>p.ProcessId===pid),marker=authorized?'run_issue_browser.py':'scripts/dev.py'
 if(!parent||String(parent.ExecutablePath).toLowerCase()!==py.toLowerCase()||!String(parent.CommandLine).includes(marker))throw Error('QA launcher ownership mismatch')
 const sockets=listeners(),ids=new Set(tree.map((p:any)=>p.ProcessId))
 if(sockets.length!==2||sockets.some((s:any)=>s.LocalAddress!=='127.0.0.1'||!ids.has(s.OwningProcess)))throw Error('QA listener ownership mismatch')
 return {pid,tree,listeners:sockets,database,authorized}
}
async function start(controlled=false){
 authorized=controlled
 const args=controlled?['-u','backend/tests/run_issue_browser.py','--output',task,'--backend-port','8023','--frontend-port','5186']:['-u','scripts/dev.py','--database',database,'--backend-port','8023','--frontend-port','5186']
 launcher=spawn(py,args,{cwd:root,windowsHide:true,stdio:['ignore','pipe','pipe']})
 launcher.stdout?.pipe(createWriteStream(path.join(artifacts,'launcher.log'),{flags:'a'}));launcher.stderr?.pipe(createWriteStream(path.join(artifacts,'launcher-error.log'),{flags:'a'}))
 await ready(apiUrl+'/api/health');await ready(uiUrl)
 if(controlled){fixtures=JSON.parse(readFileSync(path.join(task,'authorized-qa.json'),'utf8'));database=fixtures.database}
 return ownership()
}
async function stop(){
 const owned=ownership()
 execFileSync('taskkill.exe',['/PID',String(launcher.pid),'/T','/F'],{windowsHide:true,stdio:'pipe'})
 await new Promise<void>(resolve=>{if(launcher.exitCode!==null||launcher.signalCode!==null)resolve();else launcher.once('exit',()=>resolve())})
 await offline(apiUrl+'/api/health');await offline(uiUrl);expect(listeners()).toEqual([])
 evidence.stops.push({before:owned,afterListeners:[]})
}
function ddl(sql:string){execFileSync(py,['-c','import sys;from backend.app.db import Database;db=Database(sys.argv[1]);\nwith db.write() as session: session.connection().exec_driver_sql(sys.argv[2])\ndb.engine.dispose()',database,sql],{cwd:root,windowsHide:true,stdio:'pipe'})}
function snapshot(){return JSON.parse(execFileSync(py,['-c','import json,sys;from backend.tests.test_supplier_offers import snapshot;print(json.dumps(snapshot(sys.argv[1])))',database],{cwd:root,windowsHide:true,encoding:'utf8',stdio:['ignore','pipe','pipe']}))}
async function json(url:string){const r=await fetch(apiUrl+url);expect(r.status).toBe(200);return r.json()}
async function stock(){return (await json('/api/stock-positions')).find((p:any)=>p.item_id===fixtures.raws[0].id&&p.lot_code==='LOT-R8-001').on_hand_qty_base}
function watch(page:Page){page.on('pageerror',e=>evidence.pageErrors.push(e.message));page.on('dialog',d=>d.accept())}
async function create(page:Page,number:string){
 await page.getByRole('button',{name:'＋ 新增領料單',exact:true}).click();await page.locator('#mi-number').fill(number);await page.locator('#mi-work').selectOption(fixtures.orders[0].id);await page.locator('#mi-date').fill('2026-11-20')
 const response=page.waitForResponse(r=>r.url().endsWith('/api/material-issues')&&r.request().method()==='POST')
 await page.getByRole('button',{name:'儲存領料草稿',exact:true}).click();const r=await response;expect(r.status()).toBe(201)
 await expect(page.getByTestId('mi-number-value')).toHaveText(number);return r.json()
}
async function add(page:Page,qty:string){
 await page.getByRole('button',{name:'＋ 新增領料明細',exact:true}).click()
 await page.locator('#mi-material').selectOption(fixtures.raws[0].id);await page.locator('#mi-warehouse').selectOption(fixtures.warehouse.id)
 await expect(page.locator('#mi-position')).toHaveValue('')
 const p=(await json('/api/material-issues/stock-options')).find((p:any)=>p.lot_code==='LOT-R8-001')
 await page.locator('#mi-position').selectOption(p.id);await page.locator('#mi-quantity').fill(qty)
 const response=page.waitForResponse(r=>/\/api\/material-issues\/[^/]+\/lines$/.test(new URL(r.url()).pathname)&&r.request().method()==='POST')
 await page.getByRole('button',{name:'儲存領料明細',exact:true}).click();const r=await response;expect(r.status()).toBe(201);return r.json()
}
async function open(page:Page,number:string){await page.getByRole('button',{name:'開啟領料單 '+number,exact:true}).click();await expect(page.getByTestId('mi-number-value')).toHaveText(number)}
async function cancelWithoutWrites(page:Page,operation:()=>Promise<void>){
 const before=snapshot();let count=0
 const countMutation=(r:any)=>{if(['POST','PUT','DELETE','PATCH'].includes(r.method()))count++}
 page.on('request',countMutation);await operation();page.off('request',countMutation)
 expect(count).toBe(0);expect(snapshot()).toEqual(before);evidence.cancelMutations.push(count)
}
test.beforeAll(async()=>{
 expect(listeners()).toEqual([])
 execFileSync(py,['backend/tests/seed_mrp_browser.py',database,task],{cwd:root,windowsHide:true,stdio:'pipe'})
 fixtures=JSON.parse(readFileSync(path.join(task,'mrp-browser-fixtures.json'),'utf8'))
 evidence.initialOwnership=await start()
})
test.afterEach(()=>expect(evidence.pageErrors).toEqual([]))
test.afterAll(async()=>{
 try{if(launcher&&launcher.exitCode===null&&launcher.signalCode===null)await stop()}
 finally{writeFileSync(path.join(artifacts,'issue-desktop-evidence.json'),JSON.stringify(evidence,null,2))}
})

test('Desktop draft CRUD, explicit lot, multiple lines, independent edits, zero-write Cancel and F5',async({page})=>{
 watch(page);await page.goto('/#material-issues');await page.getByRole('button',{name:'＋ 新增領料單',exact:true}).waitFor()
 await cancelWithoutWrites(page,async()=>{await page.getByRole('button',{name:'＋ 新增領料單',exact:true}).click();await page.locator('#mi-number').fill('UNSAVED');await page.getByRole('button',{name:'取消',exact:true}).click()})
 draft=await create(page,'MI-DESKTOP')
 await expect(page.getByTestId('mi-state')).toHaveText('草稿／尚未領料過帳，庫存未異動')
 const first=await add(page,'1.25');const second=await add(page,'-0.000000000000000000000000000001')
 expect(await stock()).toBe('4')
 await page.getByRole('button',{name:'編輯領料明細 '+first.line_no,exact:true}).click();await page.locator('#mi-quantity').fill('0.75')
 const updated=page.waitForResponse(r=>r.request().method()==='PUT'&&r.url().includes('/lines/'));await page.getByRole('button',{name:'儲存領料明細',exact:true}).click();expect((await updated).status()).toBe(200)
 await cancelWithoutWrites(page,async()=>{await page.getByRole('button',{name:'編輯領料明細 '+first.line_no,exact:true}).click();await page.locator('#mi-quantity').fill('999');await page.getByRole('button',{name:'取消',exact:true}).click()})
 await expect(page.getByTestId('mi-line-'+first.line_no)).toContainText('0.75')
 await cancelWithoutWrites(page,async()=>{await page.getByRole('button',{name:'編輯領料表頭',exact:true}).click();await page.locator('#mi-note').fill('discard');await page.getByRole('button',{name:'取消',exact:true}).click()})
 await page.getByRole('button',{name:'刪除領料明細 '+second.line_no,exact:true}).click();await expect(page.getByTestId('mi-line-'+second.line_no)).toHaveCount(0)
 await page.reload();await expect(page.getByTestId('mi-number-value')).toHaveText('MI-DESKTOP');await expect(page.getByTestId('mi-line-'+first.line_no)).toContainText('0.75')
 expect(await stock()).toBe('4');evidence.draft={f5:true,stockBefore:'4',stockAfter:'4',lineEditsIndependent:true}
})

test('Desktop normal policy gate, real 403, source errors retain input, adjacent feedback and eligible deletion',async({page,request})=>{
 watch(page);await page.goto('/#material-issues?record='+draft.id)
 await expect(page.getByRole('button',{name:'正式領料過帳（政策待核定）',exact:true})).toBeDisabled()
 await page.getByRole('button',{name:'檢查過帳政策',exact:true}).click();await expect(page.getByRole('status')).toContainText('不能正式扣庫存')
 const before=snapshot(),row=await json('/api/material-issues/'+draft.id)
 const denied=await request.post(apiUrl+'/api/material-issues/'+draft.id+'/post?qa=true',{headers:{'X-QA-Authorized':'true'},data:{request_key:randomUUID(),row_version:row.row_version,work_order_version:row.work_order_version}})
 expect(denied.status()).toBe(403);expect(snapshot()).toEqual(before)
 ddl("CREATE TRIGGER qa_desktop_line_delete BEFORE DELETE ON material_issue_line BEGIN SELECT RAISE(ABORT,'QA row deletion rejection'); END")
 const noDelete=snapshot();await page.getByRole('button',{name:'刪除領料明細 1',exact:true}).click()
 const failedRow=page.getByTestId('mi-line-1');await expect(failedRow.getByRole('alert')).toContainText('操作已取消')
 const rowGroup=failedRow.locator('[data-feedback-group]'),rowBox=await rowGroup.boundingBox();expect(rowBox).not.toBeNull();expect(rowBox!.y).toBeGreaterThanOrEqual(0);expect(rowBox!.y+rowBox!.height).toBeLessThanOrEqual(1101)
 expect(snapshot()).toEqual(noDelete);ddl('DROP TRIGGER qa_desktop_line_delete')
 evidence.rowDeleteFeedbackAdjacent=true
 const extra=await create(page,'MI-OTHER')
 await page.getByRole('button',{name:'編輯領料表頭',exact:true}).click();await page.locator('#mi-number').fill('MI-DESKTOP')
 await page.getByRole('button',{name:'儲存表頭變更',exact:true}).click();await expect(page.getByRole('alert')).toContainText('操作已取消')
 await expect(page.locator('#mi-number')).toHaveValue('MI-DESKTOP')
 const group=page.locator('section[aria-label="領料單資料"] [data-feedback-group]'),box=await group.boundingBox()
 expect(box).not.toBeNull();expect(box!.y).toBeGreaterThanOrEqual(0);expect(box!.y+box!.height).toBeLessThanOrEqual(1101)
 await page.getByRole('button',{name:'取消',exact:true}).click();await page.getByRole('button',{name:'刪除領料草稿',exact:true}).click()
 await expect(page.getByTestId('mi-number-value')).toHaveCount(0);expect((await request.get(apiUrl+'/api/material-issues/'+extra.id)).status()).toBe(404)
 expect(await stock()).toBe('4');evidence.normalGate={httpStatus:403,noMutation:true,requestBypassRejected:true}
})

test('Desktop controlled-QA actual posting, idempotency, updated MRP and true restart with authorization disabled',async({page,request})=>{
 watch(page);await stop();evidence.controlledOwnership=await start(true)
 await page.goto('/#material-issues');const issue=await create(page,'MI-QA-POST');await add(page,'1.25')
 const preserved=await create(page,'MI-QA-PERSISTED-DRAFT');await add(page,'0.25')
 await open(page,'MI-QA-POST');expect(await stock()).toBe('4')
 const response=page.waitForResponse(r=>r.url().endsWith('/post')&&r.request().method()==='POST')
 await page.getByRole('button',{name:'QA 測試過帳（非生產核准）',exact:true}).click()
 const postedResponse=await response;expect(postedResponse.status()).toBe(201);const posted=await postedResponse.json()
 await expect(page.getByTestId('mi-state')).toContainText('實際庫存已扣除');expect(await stock()).toBe('2.75')
 await expect(page.getByRole('button',{name:'編輯領料表頭',exact:true})).toHaveCount(0);await expect(page.getByRole('button',{name:/編輯領料明細/})).toHaveCount(0)
 const body={request_key:posted.posting_request_key,row_version:posted.posting_source_version,work_order_version:posted.work_order_version}
 for(let i=0;i<2;i++)expect((await request.post(apiUrl+'/api/material-issues/'+issue.id+'/post',{data:body})).status()).toBe(200)
 const mrp=await request.post(apiUrl+'/api/mrp/preview',{data:{work_order_ids:fixtures.orders.map((o:any)=>o.id),warehouse_ids:[fixtures.warehouse.id]}})
 expect(mrp.status()).toBe(200);const result=(await mrp.json()).materials.find((m:any)=>m.material.id===fixtures.raws[0].id)
 expect([result.physical_stock_qty_base,result.shortage_qty_base]).toEqual(['2.75','8.8125'])
 await page.reload();await expect(page.getByTestId('mi-state')).toContainText('已過帳')
 await page.screenshot({path:path.join(artifacts,'desktop-posted-issue.png'),fullPage:true})
 const before=snapshot(),old=ownership();await stop();const fresh=await start(false)
 expect(fresh.pid).not.toBe(old.pid);expect(fresh.listeners.every((s:any)=>!old.listeners.some((p:any)=>p.OwningProcess===s.OwningProcess))).toBe(true)
 expect((await json('/api/material-issues/policy')).posting_allowed).toBe(false);expect(snapshot()).toEqual(before)
 await page.reload();await expect(page.getByTestId('mi-state')).toContainText('已過帳')
 expect((await request.post(apiUrl+'/api/material-issues/'+issue.id+'/post',{data:body})).status()).toBe(200)
 await open(page,'MI-QA-PERSISTED-DRAFT');await expect(page.getByTestId('mi-line-1')).toContainText('0.25')
 await expect(page.getByRole('button',{name:'正式領料過帳（政策待核定）',exact:true})).toBeDisabled()
 const draftRow=await json('/api/material-issues/'+preserved.id)
 expect((await request.post(apiUrl+'/api/material-issues/'+preserved.id+'/post',{data:{request_key:randomUUID(),row_version:draftRow.row_version,work_order_version:draftRow.work_order_version}})).status()).toBe(403)
 expect(await stock()).toBe('2.75');expect(snapshot()).toEqual(before)
 evidence.realPosting={before:'4',afterDraft:'4',afterPost:'2.75',afterRetries:'2.75',afterF5:'2.75',afterRestart:'2.75',movement:'-1.25',mrpShortage:'8.8125',postingId:posted.posting_id,postings:1}
 evidence.restart={old,fresh,zeroListeners:true,authorizationAfterRestart:false,draftPersisted:true,postedPersisted:true}
})
