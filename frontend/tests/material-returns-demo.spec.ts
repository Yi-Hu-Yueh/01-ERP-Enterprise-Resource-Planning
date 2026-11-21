import {test,expect,type Page} from '@playwright/test'
import {spawn,execFileSync,type ChildProcess} from 'node:child_process'
import {mkdirSync,createWriteStream,writeFileSync} from 'node:fs'
import path from 'node:path'
import {fileURLToPath} from 'node:url'
import {randomUUID} from 'node:crypto'

const root=path.resolve(fileURLToPath(new URL('../../',import.meta.url)))
const artifacts=path.resolve(root,process.env.ERP_TEST_ARTIFACTS||'artifacts/manual-round16b/browser/results'),task=path.dirname(artifacts)
mkdirSync(artifacts,{recursive:true})
const py=path.join(root,'.venv','Scripts','python.exe'),apiUrl='http://127.0.0.1:8028',uiUrl='http://127.0.0.1:5191'
const database=path.join(task,'material-return-demo-'+randomUUID()+'.sqlite')
type Runtime={child:ChildProcess;ports:number[];marker:string;name:string}
let runtime:Runtime,normal:Runtime|undefined,fixtures:any,draft:any,control:any,retryBody:any,posted:any
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
 if(!parent||String(parent.ExecutablePath).toLowerCase()!==py.toLowerCase()||!String(parent.CommandLine).includes(r.marker)||!String(parent.CommandLine).toLowerCase().includes(database.toLowerCase()))throw Error('Return demo QA launcher ownership mismatch; nothing stopped')
 const sockets=listeners(r.ports),ids=new Set(tree.map((p:any)=>p.ProcessId))
 if(sockets.length!==2||sockets.some((s:any)=>s.LocalAddress!=='127.0.0.1'||!ids.has(s.OwningProcess)))throw Error('Return demo QA listener ownership mismatch; nothing stopped')
 return {pid,tree,listeners:sockets,database,runtime:r.name}
}
async function start(demo=true){
 const ports=demo?[8028,5191]:[8029,5192],name=demo?'local-demo':'ordinary',marker=demo?'backend.local_demo':'scripts/dev.py'
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
async function json(url:string){const response=await fetch(apiUrl+url);expect(response.status).toBe(200);return response.json()}
async function stock(){return(await json('/api/stock-positions')).find((p:any)=>p.item_id===fixtures.raws[0].id&&p.lot_code==='LOT-R8-001').on_hand_qty_base}
function watch(page:Page){page.on('pageerror',error=>evidence.pageErrors.push(error.message))}
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
 await page.getByLabel('備註',{exact:true}).fill('Synthetic desktop local-demo confirmation QA')
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
 expect(listeners([8028,5191,8029,5192])).toEqual([])
 const output=execFileSync(py,['-c','import json,sys;from pathlib import Path;from backend.tests.material_return_qa_runtime import seed_material_return_qa_database;print(json.dumps(seed_material_return_qa_database(Path(sys.argv[1])),ensure_ascii=False))',database],{cwd:root,windowsHide:true,encoding:'utf8',env:{...process.env,PYTHONIOENCODING:'utf-8'},stdio:['ignore','pipe','pipe']})
 fixtures=JSON.parse(output);writeFileSync(path.join(artifacts,'fixture-description.json'),JSON.stringify(fixtures,null,2))
 runtime=await start();evidence.initialOwnership=ownership(runtime)
})
test.afterEach(()=>expect(evidence.pageErrors).toEqual([]))
test.afterAll(async()=>{
 try{
  if(normal&&normal.child.exitCode===null&&normal.child.signalCode===null)await stop(normal)
  if(runtime&&runtime.child.exitCode===null&&runtime.child.signalCode===null)await stop(runtime)
 }finally{writeFileSync(path.join(artifacts,'material-return-demo-desktop-evidence.json'),JSON.stringify(evidence,null,2))}
})

test('Desktop local-demo full authoritative review, draft Cancel and review Cancel/Escape/F5 make zero mutations',async({page})=>{
 watch(page);await page.goto('/#material-returns');await page.getByRole('button',{name:'＋ 新增退料單',exact:true}).waitFor()
 let mutations=0;const count=(request:any)=>{if(['POST','PUT','DELETE','PATCH'].includes(request.method()))mutations++}
 const initial=snapshot();page.on('request',count)
 await page.getByRole('button',{name:'＋ 新增退料單',exact:true}).click();await page.getByLabel('退料單號 *',{exact:true}).fill('UNSAVED')
 await page.getByRole('button',{name:'取消',exact:true}).click();expect(mutations).toBe(0);expect(snapshot()).toEqual(initial);page.off('request',count)
 draft=await create(page,'MRET-TEST-001');await add(page,'0.75');expect(await stock()).toBe('2.25')
 await page.getByRole('button',{name:'展開編輯退料明細 1',exact:true}).click();await page.getByLabel('本次退料量 *',{exact:true}).fill('9.99')
 mutations=0;const beforeLine=snapshot();page.on('request',count);await page.getByRole('button',{name:'取消',exact:true}).click()
 expect(mutations).toBe(0);expect(snapshot()).toEqual(beforeLine);page.off('request',count);await expect(page.getByTestId('mr-line-1')).toContainText('本次 0.75')
 control=await create(page,'MRET-RESTART-DRAFT');await add(page,'0.25');await open(page)
 expect((await json('/api/material-issues/policy')).authorization_basis).toBe('LOCAL_DEMO_MANUAL')
 expect((await json('/api/material-returns/policy')).authorization_basis).toBe('LOCAL_DEMO_MANUAL')
 await expect(page.getByTestId('mr-policy')).toContainText('本機展示 · 人工確認退料')
 const before=snapshot();mutations=0;page.on('request',count)
 const dialog=await review(page)
 for(const value of ['MRET-TEST-001','ISSUE-TEST-001 第 1 行',fixtures.orders[0].document_no,fixtures.raws[0].code,fixtures.warehouse.code,'LOT-R8-001','原領 1.75','已退 0','可退 1.75','本行 0.75','+0.75','2.25','3','本單後可退 1'])await expect(dialog).toContainText(value)
 await expect(dialog.getByText('本機展示模式／尚未啟用正式電子簽核與身分權限驗證',{exact:true})).toBeVisible()
 await expect(dialog.getByRole('button',{name:'確認並增加庫存',exact:true})).toBeEnabled();await expect(dialog.getByRole('button',{name:'取消',exact:true})).toBeFocused()
 expect((await dialog.boundingBox())?.width).toBeGreaterThan(1000);await dialog.screenshot({path:path.join(artifacts,'desktop-local-demo-return-review.png')})
 expect(snapshot()).toEqual(before);await dialog.getByRole('button',{name:'取消',exact:true}).click();await expect(dialog).toHaveCount(0)
 await review(page);await page.keyboard.press('Escape');await expect(page.getByRole('dialog')).toHaveCount(0)
 await review(page);await page.reload();await expect(page.getByTestId('mr-state')).toContainText('草稿');await expect(page.getByRole('dialog')).toHaveCount(0)
 await page.getByRole('button',{name:'重新讀取領料來源與庫存',exact:true}).click();await expect(page.getByRole('status').filter({hasText:'已重新讀取已過帳來源'})).toBeVisible()
 page.off('request',count);expect(mutations).toBe(0);expect(snapshot()).toEqual(before);expect(await stock()).toBe('2.25')
 evidence.cancelMutationCounts.push(0,0,mutations);evidence.review={sourceIssue:'ISSUE-TEST-001',sourceLine:1,workOrder:fixtures.orders[0].document_no,material:fixtures.raws[0].code,warehouse:fixtures.warehouse.code,lot:'LOT-R8-001',issued:'1.75',priorReturned:'0',proposed:'0.75',remainingAfter:'1',stock:'2.25',movement:'+0.75',expectedStock:'3',cancel:true,escape:true,f5WhileReviewOpen:true,mutationRequests:mutations}
})

test('Desktop actual LOCAL_DEMO_MANUAL return posts +0.75 once, double-click and original retry preserve exact stock3',async({page,request})=>{
 watch(page);await open(page);const dialog=await review(page);expect(await stock()).toBe('2.25')
 const bodies:any[]=[];let first=true
 await page.route('**/api/material-returns/'+draft.id+'/post',async route=>{
  bodies.push(route.request().postDataJSON());const response=await route.fetch()
  if(first){first=false;expect(response.status()).toBe(201);await new Promise(resolve=>setTimeout(resolve,250));await route.abort('failed')}
  else{expect(response.status()).toBe(200);await route.fulfill({response})}
 })
 await dialog.getByRole('button',{name:'確認並增加庫存',exact:true}).dblclick({force:true})
 await expect(dialog.getByRole('alert')).toContainText('保留原請求重試');expect(bodies).toHaveLength(1);expect(await stock()).toBe('3')
 await dialog.getByRole('button',{name:'取消',exact:true}).click();await expect(page.getByRole('status').filter({hasText:'上次請求可能已提交'})).toBeVisible()
 const reopened=await review(page);await reopened.getByRole('button',{name:'原請求重試並核對結果',exact:true}).click();await expect(reopened).toHaveCount(0)
 expect(bodies).toHaveLength(2);expect(bodies[1]).toEqual(bodies[0]);retryBody=bodies[0]
 posted=await json('/api/material-returns/'+draft.id);expect(posted.authorization_basis).toBe('LOCAL_DEMO_MANUAL');expect(posted.posting_state).toBe('POSTED')
 expect(posted.movements).toHaveLength(1);expect(posted.movements[0].qty_base_delta).toBe('0.75')
 const line=posted.lines[0];expect([line.source.issued_qty_base,line.source.returned_qty_base,line.source.returnable_qty_base,line.proposed_qty_base,line.remaining_after_proposal_qty_base]).toEqual(['1.75','0.75','1','0.75','1'])
 expect([line.source.issue_id,line.origin_line_id,line.source.work_order_id,line.source.position.id]).toEqual([fixtures.issue.id,fixtures.issue.lines[0].id,fixtures.orders[0].id,fixtures.issue.lines[0].position_id])
 await expect(page.getByTestId('mr-state')).toContainText('實際庫存已增加');await expect(page.getByTestId('mr-movements')).toContainText('+0.75');await expect(page.getByTestId('mr-movements')).toContainText('目前實體庫存 3')
 await expect(page.getByRole('button',{name:'編輯退料表頭',exact:true})).toHaveCount(0);await expect(page.getByRole('button',{name:/展開編輯退料明細/})).toHaveCount(0)
 const before=snapshot();expect((await request.post(apiUrl+'/api/material-returns/'+draft.id+'/post',{data:retryBody})).status()).toBe(200)
 expect((await request.post(apiUrl+'/api/material-returns/'+draft.id+'/post',{data:{...retryBody,row_version:retryBody.row_version+1}})).status()).toBe(409)
 expect((await request.post(apiUrl+'/api/material-returns/'+draft.id+'/post',{data:{...retryBody,request_key:randomUUID()}})).status()).toBe(409)
 expect((await request.put(apiUrl+'/api/material-returns/'+draft.id,{data:{document_no:posted.document_no,work_order_id:posted.work_order_id,work_order_version:posted.work_order_version,business_date:posted.business_date,row_version:posted.row_version,note:'Rejected edit'}})).status()).toBe(409)
 expect((await request.delete(apiUrl+'/api/material-returns/'+draft.id+'?row_version='+posted.row_version)).status()).toBe(409)
 expect(snapshot()).toEqual(before);const issue=await json('/api/material-issues/'+fixtures.issue.id);expect(issue.movements).toHaveLength(1);expect(issue.movements[0].qty_base_delta).toBe('-1.75')
 await page.reload();await expect(page.getByTestId('mr-state')).toContainText('已過帳');expect(await stock()).toBe('3')
 await page.screenshot({path:path.join(artifacts,'desktop-local-demo-return-posted.png'),fullPage:true})
 evidence.postingRequests=bodies.length;evidence.posting={basis:posted.authorization_basis,stockBefore:'2.25',movement:'+0.75',stockAfter:'3',originalMovement:'-1.75',cumulativeReturned:'0.75',remainingReturnable:'1',oneMovement:true,duplicateClickRequests:1,uncertainCommittedResponse:true,exactOriginalRetry:true,conflictStatus:409,postedEditDeleteStatus:409,f5:true}
})

test('Desktop genuine frontend/backend stop and new-process restart preserve posting, draft, readers and safe overreturn rejection',async({page,request})=>{
 watch(page);const before=snapshot(),old=ownership(runtime);await stop(runtime);runtime=await start();const fresh=ownership(runtime)
 expect(fresh.pid).not.toBe(old.pid);expect(fresh.listeners.every((socket:any)=>!old.listeners.some((prior:any)=>prior.OwningProcess===socket.OwningProcess))).toBe(true)
 await open(page);await expect(page.getByTestId('mr-state')).toContainText('已過帳');expect(await stock()).toBe('3')
 expect((await request.post(apiUrl+'/api/material-returns/'+draft.id+'/post',{data:retryBody})).status()).toBe(200);expect(snapshot()).toEqual(before)
 await open(page,control);await expect(page.getByTestId('mr-state')).toContainText('草稿');await expect(page.getByTestId('mr-line-1')).toContainText('本次 0.25');expect(snapshot()).toEqual(before)
 const source=(await json('/api/material-returns/sources')).find((s:any)=>s.origin_line_id===fixtures.issue.lines[0].id);expect([source.issued_qty_base,source.returned_qty_base,source.returnable_qty_base]).toEqual(['1.75','0.75','1'])
 const issue=await json('/api/material-issues/'+fixtures.issue.id);expect(issue.movements[0].qty_base_delta).toBe('-1.75');expect(issue.lines[0].position.physical_qty_base).toBe('3')
 const supplierSources=await json('/api/supplier-returns/source-receipts'),supplierLines=supplierSources.flatMap((r:any)=>r.lines).filter((line:any)=>line.item_id===fixtures.raws[0].id&&line.lot_code==='LOT-R8-001')
 expect(supplierLines.length).toBeGreaterThan(0);for(const line of supplierLines)expect(line.on_hand_qty_base).toBe('3')
 const mrp=await request.post(apiUrl+'/api/mrp/preview',{data:{work_order_ids:fixtures.orders.map((work:any)=>work.id),warehouse_ids:[fixtures.warehouse.id]}});expect(mrp.status()).toBe(200)
 const preview=await mrp.json(),material=preview.materials.find((m:any)=>m.material.id===fixtures.raws[0].id);expect([material.gross_demand_qty_base,material.physical_stock_qty_base,material.shortage_qty_base]).toEqual(['11.5625','3','8.5625']);expect(preview.preliminary).toBe(true)
 await create(page,'MRET-OVERRETURN-QA');await add(page,'2');const overBefore=snapshot();await page.getByRole('button',{name:'檢視退料過帳',exact:true}).click()
 await expect(page.getByRole('alert')).toContainText('超過剩餘可退量');await expect(page.getByRole('dialog')).toHaveCount(0);expect(snapshot()).toEqual(overBefore);expect(await stock()).toBe('3')
 await page.screenshot({path:path.join(artifacts,'desktop-local-demo-return-overreturn.png'),fullPage:true})
 evidence.restart={old,fresh,zeroListenersObserved:true,durablePosted:true,durableOriginalRetry:true,durableDraft:true,stock:'3'}
 evidence.readers={issue:'-1.75',currentSourceStock:'3',supplierSourceStock:'3',mrpGross:'11.5625',mrpPhysical:'3',preliminaryDifference:'8.5625',notRemainingProcurement:true,overreturnReviewStatus:422,noPartialMutation:true}
})

test('Desktop ordinary runtime stays closed despite demo tokens and spoof flags; issue and return confirmations cannot be borrowed',async({page,playwright})=>{
 // Snapshot/process ownership checks outlast uvicorn's keep-alive window.
 // Use fresh HTTP connections here, without retrying rejected posting requests.
 const request=await playwright.request.newContext({extraHTTPHeaders:{Connection:'close'}})
 try{
 watch(page);await open(page,control)
 const returnReview=await json('/api/material-returns/'+control.id+'/posting-review'),returnBody={request_key:randomUUID(),row_version:returnReview.material_return.row_version,work_order_version:returnReview.material_return.work_order_version,confirmation_token:returnReview.confirmation_token}
 const issueHeader=await request.post(apiUrl+'/api/material-issues',{data:{request_key:randomUUID(),document_no:'ISSUE-BOUNDARY-DRAFT',work_order_id:fixtures.orders[0].id,work_order_version:fixtures.orders[0].row_version,business_date:'2026-11-20',note:'Synthetic grant separation control'}});expect(issueHeader.status()).toBe(201)
 const issue=await issueHeader.json(),source=fixtures.issue.lines[0]
 expect((await request.post(apiUrl+'/api/material-issues/'+issue.id+'/lines',{data:{request_key:randomUUID(),document_version:issue.row_version,material_id:source.material_id,warehouse_id:source.warehouse_id,position_id:source.position_id,quantity:'0.25',note:null,bom_component_id:null,bom_component_version:null}})).status()).toBe(201)
 const issueReview=await json('/api/material-issues/'+issue.id+'/posting-review'),issueBody={request_key:randomUUID(),row_version:issueReview.issue.row_version,work_order_version:issueReview.issue.work_order_version,confirmation_token:issueReview.confirmation_token}
 const before=snapshot()
 expect((await request.post(apiUrl+'/api/material-returns/'+control.id+'/post',{data:{...returnBody,confirmation_token:issueReview.confirmation_token}})).status()).toBe(403)
 expect((await request.post(apiUrl+'/api/material-issues/'+issue.id+'/post',{data:{...issueBody,confirmation_token:returnReview.confirmation_token}})).status()).toBe(403)
 normal=await start(false);evidence.ordinaryOwnership=ownership(normal)
 for(const operation of ['material-issues','material-returns']){const response=await request.get('http://127.0.0.1:8029/api/'+operation+'/policy');expect(response.status()).toBe(200);expect((await response.json()).posting_allowed).toBe(false)}
 await page.goto('http://127.0.0.1:5192/#material-returns?record='+control.id);await expect(page.getByTestId('mr-policy')).toContainText('CLOSED')
 const dialog=await review(page);await expect(dialog.getByRole('button',{name:'確認並增加庫存',exact:true})).toBeDisabled();await dialog.getByRole('button',{name:'取消',exact:true}).click()
 expect((await request.post('http://127.0.0.1:8029/api/material-returns/'+control.id+'/post?authorized=true&local_demo=true',{headers:{'X-Local-Demo':'true','X-Authorized':'true'},data:returnBody})).status()).toBe(403)
 expect((await request.post('http://127.0.0.1:8029/api/material-issues/'+issue.id+'/post?authorized=true',{headers:{'X-Local-Demo':'true'},data:issueBody})).status()).toBe(403)
 expect(snapshot()).toEqual(before);expect(await stock()).toBe('3')
 await page.screenshot({path:path.join(artifacts,'desktop-ordinary-return-closed.png'),fullPage:true})
 evidence.authorization={demoIssue:true,demoReturn:true,ordinaryIssue:false,ordinaryReturn:false,ordinaryWithDemoTokenAndFlags:403,borrowedIssueTokenForReturn:403,borrowedReturnTokenForIssue:403,ordinaryUiConfirmDisabled:true,noMutations:true,stock:'3'}
 }finally{await request.dispose()}
})
