import {test,expect,type Page} from '@playwright/test'
import {spawn,execFileSync,type ChildProcess} from 'node:child_process'
import {mkdirSync,createWriteStream,writeFileSync,readFileSync} from 'node:fs'
import path from 'node:path'
import {fileURLToPath} from 'node:url'
import {randomUUID} from 'node:crypto'
import {expectActionFeedback} from './feedback-assertions'

const root=path.resolve(fileURLToPath(new URL('../../',import.meta.url)))
const artifacts=path.resolve(root,process.env.ERP_TEST_ARTIFACTS||'artifacts/manual-round13/browser/results'),task=path.dirname(artifacts)
mkdirSync(artifacts,{recursive:true})
const database=path.join(task,'browser-work-orders-'+randomUUID()+'.sqlite')
const py=path.join(root,'.venv','Scripts','python.exe'),apiUrl='http://127.0.0.1:8021',uiUrl='http://127.0.0.1:5184'
let launcher:ChildProcess,fixtures:any,before:any,none:any,bomOnly:any,routingOnly:any,both:any
const children:ChildProcess[]=[],evidence:Record<string,any>={isolated:true,database,urls:{apiUrl,uiUrl},generatedScreenshots:0,pageErrors:[],ownershipStops:[]}
const precise='0.'+'0'.repeat(90)+'12345678901234567890123456789'
test.use({baseURL:uiUrl,screenshot:'off',trace:'off'})
test.describe.configure({mode:'serial'})
async function ready(url:string){await expect.poll(async()=>{try{return(await fetch(url,{signal:AbortSignal.timeout(1000)})).status}catch{return 0}},{timeout:40_000}).toBe(200)}
async function offline(url:string){await expect.poll(async()=>{try{return(await fetch(url,{signal:AbortSignal.timeout(1000)})).status}catch{return 0}},{timeout:20_000}).toBe(0)}
function ps(script:string){return JSON.parse(execFileSync('powershell.exe',['-NoProfile','-Command',script],{windowsHide:true,encoding:'utf8',stdio:['ignore','pipe','pipe']}))}
function listeners(){return ps('ConvertTo-Json -InputObject @(Get-NetTCPConnection -State Listen | Where-Object LocalPort -in 8021,5184 | Select-Object LocalAddress,LocalPort,OwningProcess) -Compress')}
function ownership(child:ChildProcess){
 const pid=Number(child.pid)
 if(!Number.isInteger(pid))throw Error('No owned QA launcher PID')
 const tree=ps('$qaProcesses=Get-CimInstance Win32_Process; $qaIds=@('+pid+'); do {$qaChildren=@($qaProcesses | Where-Object {$_.ParentProcessId -in $qaIds -and $_.ProcessId -notin $qaIds} | Select-Object -ExpandProperty ProcessId); $qaIds+=$qaChildren}while($qaChildren.Count -gt 0); ConvertTo-Json -InputObject @($qaProcesses | Where-Object ProcessId -in $qaIds | Select-Object ProcessId,ParentProcessId,ExecutablePath,CommandLine,CreationDate) -Depth 5 -Compress')
 const parent=tree.find((p:any)=>p.ProcessId===pid)
 if(!parent||String(parent.ExecutablePath).toLowerCase()!==py.toLowerCase()||!String(parent.CommandLine).toLowerCase().includes(database.toLowerCase())||!/scripts[\\/]dev\.py/.test(parent.CommandLine))throw Error('QA launcher ownership mismatch; nothing stopped')
 for(const p of tree){const project=String(p.CommandLine).toLowerCase().includes(root.toLowerCase())||String(p.ExecutablePath).toLowerCase().startsWith(root.toLowerCase()+path.sep);const consoleChild=/[\\/]conhost\.exe$/i.test(String(p.ExecutablePath));if(!project&&!consoleChild)throw Error('Unverified QA descendant; nothing stopped')}
 const sockets=listeners(),ids=new Set(tree.map((p:any)=>p.ProcessId))
 if(sockets.length!==2||sockets.some((s:any)=>s.LocalAddress!=='127.0.0.1'||!ids.has(s.OwningProcess)))throw Error('QA listener ownership mismatch; nothing stopped')
 return {rootPid:pid,tree,listeners:sockets}
}
async function start(){
 launcher=spawn(py,['-u','scripts/dev.py','--backend-port','8021','--frontend-port','5184','--database',database],{cwd:root,windowsHide:true,stdio:['ignore','pipe','pipe']});children.push(launcher)
 launcher.stdout?.pipe(createWriteStream(path.join(artifacts,'launcher.log'),{flags:'a'}));launcher.stderr?.pipe(createWriteStream(path.join(artifacts,'launcher-error.log'),{flags:'a'}))
 await ready(apiUrl+'/api/health');await ready(uiUrl);expect(launcher.exitCode).toBeNull();return ownership(launcher)
}
async function stop(child:ChildProcess){
 if(child.exitCode!==null||child.signalCode!==null)throw Error('Owned QA launcher exited unexpectedly')
 const owned=ownership(child)
 execFileSync('taskkill.exe',['/PID',String(child.pid),'/T','/F'],{windowsHide:true,stdio:'pipe'})
 await new Promise<void>(resolve=>{if(child.exitCode!==null||child.signalCode!==null)resolve();else child.once('exit',()=>resolve())})
 await offline(apiUrl+'/api/health');await offline(uiUrl);expect(listeners()).toEqual([])
 evidence.ownershipStops.push({before:owned,afterListeners:[]})
}
function snapshot(){return JSON.parse(execFileSync(py,['-c','import json,sys;from backend.tests.test_supplier_offers import snapshot;print(json.dumps(snapshot(sys.argv[1])))',database],{cwd:root,windowsHide:true,encoding:'utf8',stdio:['ignore','pipe','pipe']}))}
function legacy(s:any){return Object.fromEntries(Object.entries(s.rows).filter(([t])=>!['work_order','stock_create_receipt'].includes(t)))}
async function json(url='/api/work-orders'){const r=await fetch(uiUrl+url);expect(r.status).toBe(200);return r.json()}
function watch(page:Page){page.on('pageerror',e=>{evidence.pageErrors.push(e.message)})}
async function open(page:Page,row:any){await page.getByRole('button',{name:'開啟工單 '+row.document_no,exact:true}).click();await expect(page.getByTestId('wo-number-value')).toHaveText(row.document_no)}
async function newOrder(page:Page,number:string,product:number,quantity:string,bom:number|null=null,routing:number|null=null,start='2026-11-20',finish='2026-11-25'){
 await page.getByRole('button',{name:'＋ 新增工單',exact:true}).click()
 await page.locator('#wo-number').fill(number);await page.locator('#wo-product').selectOption(fixtures.products[product].id)
 await expect(page.locator('#wo-bom')).toHaveValue('');await expect(page.locator('#wo-routing')).toHaveValue('')
 await page.locator('#wo-qty').fill(quantity);await page.locator('#wo-start').fill(start);await page.locator('#wo-finish').fill(finish);await page.locator('#wo-note').fill('隔離測試 '+number)
 if(bom!==null)await page.locator('#wo-bom').selectOption(fixtures.boms[bom].id)
 if(routing!==null)await page.locator('#wo-routing').selectOption(fixtures.routings[routing].id)
 const response=page.waitForResponse(r=>r.url().endsWith('/api/work-orders')&&r.request().method()==='POST')
 await page.getByRole('button',{name:'儲存工單',exact:true}).click();const r=await response;expect(r.status()).toBe(201)
 await expect(page.getByTestId('wo-number-value')).toHaveText(number);return r.json()
}
function createBody(number:string,product=0,patch:Record<string,any>={}){return {document_no:number,product_id:fixtures.products[product].id,planned_qty_base:'1',planned_start:'2026-11-20',planned_finish:'2026-11-25',note:null,bom_id:null,bom_version:null,routing_id:null,routing_version:null,request_key:randomUUID(),...patch}}
function editBody(row:any,patch:Record<string,any>={}){return {document_no:row.document_no,product_id:row.product_id,planned_qty_base:row.planned_qty_base,planned_start:row.planned_start,planned_finish:row.planned_finish,note:row.note,bom_id:row.bom_id,bom_version:row.bom?.row_version??null,routing_id:row.routing_id,routing_version:row.routing?.row_version??null,row_version:row.row_version,...patch}}
async function search(page:Page,value:string){await page.locator('#wo-search').fill(value);const response=page.waitForResponse(r=>r.url().includes('/api/work-orders?q='));await page.getByRole('button',{name:'搜尋',exact:true}).click();await response}

test.beforeAll(async()=>{
 if(listeners().length)throw Error('QA port occupied; no process stopped')
 const output=execFileSync(py,[path.join(root,'backend/tests/seed_work_orders_browser.py'),database,task],{cwd:root,windowsHide:true,encoding:'utf8',stdio:['ignore','pipe','pipe']})
 writeFileSync(path.join(artifacts,'seed-output.txt'),output);fixtures=JSON.parse(readFileSync(path.join(task,'work-order-browser-fixtures.json'),'utf8'))
 before=snapshot();evidence.initialOwnership=await start();evidence.beforeBusinessRows=legacy(before)
})
test.afterEach(()=>{expect(evidence.pageErrors).toEqual([])})
test.afterAll(async()=>{
 for(const child of [...children].reverse())if(child.exitCode===null&&child.signalCode===null)await stop(child)
 const after=snapshot();evidence.finalSnapshot=after;evidence.afterBusinessRows=legacy(after);evidence.qaServicesStopped=true
 writeFileSync(path.join(artifacts,'work-order-browser-evidence.json'),JSON.stringify(evidence,null,2)+'\n')
 expect(legacy(after)).toEqual(legacy(before));expect(after.objects).toEqual(before.objects)
 expect(after.rows.bom).toEqual(before.rows.bom);expect(after.rows.bom_component).toEqual(before.rows.bom_component)
 expect(after.rows.routing).toEqual(before.rows.routing);expect(after.rows.routing_step).toEqual(before.rows.routing_step)
})

test('desktop independent work orders, explicit optional sources, signed exact quantities, dates and real F5',async({page})=>{
 watch(page);await page.goto('/#work-orders');await expect(page.getByRole('heading',{name:'製造工單管理',exact:true})).toBeVisible()
 await expect(page.getByRole('heading',{name:'尚無製造工單',exact:true})).toBeVisible();expect(await page.locator('#wo-start').getAttribute('type')).toBe('date');expect(await page.locator('#wo-finish').getAttribute('type')).toBe('date')
 none=await newOrder(page,'WO-NONE',0,'0',null,null,'2026-12-20','2026-01-01');expect(none.bom).toBeNull();expect(none.routing).toBeNull();expect(none.planned_qty_base).toBe('0')
 await expect(page.getByTestId('wo-date-warning')).toContainText('待核定')
 bomOnly=await newOrder(page,'WO-BOM-ONLY',0,precise,0);expect(bomOnly.bom.id).toBe(fixtures.boms[0].id);expect(bomOnly.routing).toBeNull();expect(bomOnly.planned_qty_base).toBe(precise)
 routingOnly=await newOrder(page,'WO-ROUTING-ONLY',0,'-2.75',null,0);expect(routingOnly.bom).toBeNull();expect(routingOnly.routing.id).toBe(fixtures.routings[0].id)
 both=await newOrder(page,'WO-BOTH',1,'12.500000000000000000000000001',1,1);expect(both.product_id).toBe(fixtures.products[1].id);expect(both.bom.release_state).toBe('DRAFT');expect(both.routing.release_state).toBe('DRAFT')
 const others=await Promise.all([bomOnly,routingOnly,both].map(r=>json('/api/work-orders/'+r.id)))
 await open(page,none);await page.getByRole('button',{name:'編輯工單',exact:true}).click();await page.locator('#wo-qty').fill('-3.125');await page.locator('#wo-start').fill('2026-11-21');await page.locator('#wo-finish').fill('2026-11-26');await page.locator('#wo-bom').selectOption(fixtures.boms[0].id);await page.locator('#wo-routing').selectOption(fixtures.routings[0].id)
 const changed=page.waitForResponse(r=>r.url().endsWith('/api/work-orders/'+none.id)&&r.request().method()==='PUT');await page.getByRole('button',{name:'儲存變更',exact:true}).click();expect((await changed).status()).toBe(200)
 await expect(page.getByTestId('wo-planned-quantity')).toContainText('-3.125 UNIT-BOOK');await expect(page.getByTestId('wo-planned-start')).toHaveText('2026-11-21');await expect(page.getByTestId('wo-planned-finish')).toHaveText('2026-11-26')
 expect(await Promise.all([bomOnly,routingOnly,both].map(r=>json('/api/work-orders/'+r.id)))).toEqual(others)
 await page.getByRole('link',{name:'查看BOM ↗',exact:true}).click();await expect(page.getByTestId('bom-usage-1')).toContainText('2.75');await expect(page.getByTestId('bom-usage-2')).toContainText('0.5')
 await page.goto('/#work-orders?record='+none.id);await expect(page.getByTestId('wo-number-value')).toHaveText('WO-NONE')
 await search(page,'FG-001');await expect(page.getByRole('button',{name:/開啟工單 /})).toHaveCount(3);await search(page,'')
 const saved=await json(),savedSnapshot=snapshot();await page.reload({waitUntil:'networkidle'})
 expect(await page.evaluate(()=>performance.getEntriesByType('navigation').map(e=>(e as PerformanceNavigationTiming).type))).toContain('reload')
 await expect(page.getByTestId('wo-number-value')).toHaveText('WO-NONE');await expect(page.getByTestId('wo-planned-quantity')).toContainText('-3.125');expect(await json()).toEqual(saved);expect(snapshot()).toEqual(savedSnapshot)
 expect(await json('/api/boms')).toEqual(fixtures.masterResponses.boms);expect(await json('/api/routings')).toEqual(fixtures.masterResponses.routings)
 evidence.desktopIndependentOrdersAndOptionalSources=true;evidence.signedZeroAndPreciseQuantity=true;evidence.realDocumentReload=true;evidence.savedSelectedHash=await page.evaluate(()=>location.hash)
})

test('Cancel has zero writes, invalid/stale association requests reject, conflicts retain input and eligible drafts delete',async({page,request})=>{
 watch(page);await page.goto('/#work-orders');await open(page,both);const cancelBefore=snapshot();let mutations=0
 const count=(r:any)=>{if(['POST','PUT','DELETE','PATCH'].includes(r.method()))mutations++};page.on('request',count)
 await page.getByRole('button',{name:'編輯工單',exact:true}).click();await page.locator('#wo-qty').fill('999');await page.locator('#wo-note').fill('未儲存');await page.getByRole('button',{name:'取消',exact:true}).click()
 await expect(page.getByTestId('wo-planned-quantity')).toContainText('12.500000000000000000000000001')
 await page.getByRole('button',{name:'編輯工單',exact:true}).click();await page.locator('#wo-product').selectOption(fixtures.products[0].id)
 await expect(page.locator('#wo-bom')).toHaveValue(fixtures.boms[1].id);await expect(page.locator('#wo-bom option[value="'+fixtures.boms[1].id+'"]')).toBeDisabled();await expect(page.locator('#wo-bom option[value="'+fixtures.boms[0].id+'"]')).toHaveCount(1)
 await page.getByRole('button',{name:'儲存變更',exact:true}).click();await expect(page.getByRole('alert')).toContainText('不屬於目前成品');await expectActionFeedback(page,'section[aria-label="製造工單資料"] .form-actions')
 await expect(page.locator('#wo-product')).toHaveValue(fixtures.products[0].id);await page.getByRole('button',{name:'取消',exact:true}).click()
 await page.getByRole('button',{name:'＋ 新增工單',exact:true}).click();await page.locator('#wo-number').fill('UNSAVED');await page.locator('#wo-qty').fill('22');await page.getByRole('button',{name:'取消',exact:true}).click();await expect(page.locator('#wo-number')).toHaveValue('')
 expect(mutations).toBe(0);expect(snapshot()).toEqual(cancelBefore);page.off('request',count);evidence.desktopCancelMutations=mutations
 const negativeBefore=snapshot(),negative:any[]=[]
 for(const [label,body,status] of [
  ['cross-product BOM',createBody('BAD-BOM',0,{bom_id:fixtures.boms[1].id,bom_version:fixtures.boms[1].row_version}),422],
  ['cross-product routing',createBody('BAD-ROUTE',0,{routing_id:fixtures.routings[1].id,routing_version:fixtures.routings[1].row_version}),422],
  ['stale source version',createBody('STALE-SOURCE',0,{bom_id:fixtures.boms[0].id,bom_version:fixtures.boms[0].row_version-1}),409]
 ] as const){const r=await request.post(uiUrl+'/api/work-orders',{data:body});expect(r.status()).toBe(status);negative.push({label,status:r.status(),message:(await r.json()).message})}
 expect(snapshot()).toEqual(negativeBefore);evidence.realNegativeAssociationRequests=negative
 const b=fixtures.boms[0],route=fixtures.routings[0],step=route.steps[0]
 const heldBom=await request.put(uiUrl+'/api/boms/'+b.id,{data:{product_id:b.product_id,revision:'HELD-MUST-REJECT',output_basis_qty:b.output_basis_qty,valid_from:b.valid_from,valid_to:b.valid_to,is_active:b.is_active,row_version:b.row_version}});expect(heldBom.status()).toBe(409)
 const heldStep=await request.put(uiUrl+'/api/routings/'+route.id+'/steps/'+step.id,{data:{sequence_no:step.sequence_no,operation_id:step.operation_id,work_center_id:step.work_center_id,setup_minutes:'999',run_minutes_per_base:step.run_minutes_per_base,row_version:step.row_version,routing_version:route.row_version}});expect(heldStep.status()).toBe(409)
 expect(snapshot()).toEqual(negativeBefore);evidence.realReferencedSourceProtection=true
 await open(page,bomOnly);await page.getByRole('button',{name:'編輯工單',exact:true}).click();await page.locator('#wo-qty').fill('NaN')
 const invalid=page.waitForResponse(r=>r.url().endsWith('/api/work-orders/'+bomOnly.id)&&r.request().method()==='PUT');await page.getByRole('button',{name:'儲存變更',exact:true}).click();expect((await invalid).status()).toBe(422)
 await expect(page.locator('#wo-qty')).toHaveValue('NaN');await expectActionFeedback(page,'section[aria-label="製造工單資料"] .form-actions');expect(snapshot()).toEqual(negativeBefore)
 await page.getByRole('button',{name:'取消',exact:true}).click();await page.getByRole('button',{name:'編輯工單',exact:true}).click();await page.locator('#wo-note').fill('本視窗尚未儲存的值')
 const current=await json('/api/work-orders/'+bomOnly.id),outside=await request.put(uiUrl+'/api/work-orders/'+bomOnly.id,{data:editBody(current,{note:'另一視窗已儲存'})});expect(outside.status()).toBe(200)
 const stale=page.waitForResponse(r=>r.url().endsWith('/api/work-orders/'+bomOnly.id)&&r.request().method()==='PUT');await page.getByRole('button',{name:'儲存變更',exact:true}).click();expect((await stale).status()).toBe(409)
 await expect(page.locator('#wo-note')).toHaveValue('本視窗尚未儲存的值');await expectActionFeedback(page,'section[aria-label="製造工單資料"] .form-actions')
 expect((await json('/api/work-orders/'+bomOnly.id)).note).toBe('另一視窗已儲存');await page.getByRole('button',{name:'取消',exact:true}).click();await page.getByRole('button',{name:'重新讀取工單',exact:true}).click();await expect(page.getByTestId('wo-note-value')).toHaveText('另一視窗已儲存')
 const temporary=await newOrder(page,'WO-DELETE',0,'1');page.once('dialog',d=>d.accept());const deletion=page.waitForResponse(r=>r.url().includes('/api/work-orders/'+temporary.id)&&r.request().method()==='DELETE');await page.getByRole('button',{name:'刪除工單',exact:true}).click();expect((await deletion).status()).toBe(204)
 await expect(page.locator('#wo-number')).toHaveValue('');expect((await request.get(uiUrl+'/api/work-orders/'+temporary.id)).status()).toBe(404)
 evidence.validationAndStaleConflictPreserveInputs=true;evidence.eligibleDraftDeletion=true;evidence.beforeRestart={orders:await json(),snapshot:snapshot(),ownership:ownership(launcher)}
})

test('complete owned QA restart and mobile exact editing, Cancel, source filtering and existing navigation',async({page})=>{
 watch(page);await stop(launcher);evidence.offlineListeners=listeners();const afterOwnership=await start()
 expect(afterOwnership.listeners.every((r:any)=>!evidence.beforeRestart.ownership.listeners.some((p:any)=>p.OwningProcess===r.OwningProcess))).toBe(true)
 expect(await json()).toEqual(evidence.beforeRestart.orders);expect(snapshot()).toEqual(evidence.beforeRestart.snapshot)
 await page.setViewportSize({width:390,height:844});await page.goto('/#work-orders?record='+none.id);await expect(page.getByTestId('wo-number-value')).toHaveText('WO-NONE')
 await expect(page.getByTestId('wo-planned-start')).toHaveText('2026-11-21');await expect(page.getByTestId('wo-planned-finish')).toHaveText('2026-11-26');expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true)
 const cancelBefore=snapshot();let mutations=0;const count=(r:any)=>{if(['POST','PUT','DELETE','PATCH'].includes(r.method()))mutations++};page.on('request',count)
 await page.getByRole('button',{name:'編輯工單',exact:true}).click();await page.locator('#wo-qty').fill('999');await page.locator('#wo-finish').fill('2027-01-01')
 await expect(page.getByRole('button',{name:'取消',exact:true})).toBeVisible();await page.getByRole('button',{name:'取消',exact:true}).click()
 expect(mutations).toBe(0);expect(snapshot()).toEqual(cancelBefore);page.off('request',count)
 await page.getByRole('button',{name:'編輯工單',exact:true}).click()
 expect(await page.locator('#wo-bom option:not([disabled])').evaluateAll(options=>options.map(o=>(o as HTMLOptionElement).value))).toEqual(['',fixtures.boms[0].id])
 expect(await page.locator('#wo-routing option:not([disabled])').evaluateAll(options=>options.map(o=>(o as HTMLOptionElement).value))).toEqual(['',fixtures.routings[0].id])
 const mobileQty='-0.0000000000000000000000000000000000000000000000000000125'
 await page.locator('#wo-qty').fill(mobileQty);await page.locator('#wo-start').fill('2026-12-31');await page.locator('#wo-finish').fill('2026-01-01')
 const saved=page.waitForResponse(r=>r.url().endsWith('/api/work-orders/'+none.id)&&r.request().method()==='PUT');await page.getByRole('button',{name:'儲存變更',exact:true}).click();expect((await saved).status()).toBe(200)
 await expect(page.getByTestId('wo-planned-quantity')).toContainText(mobileQty);await expect(page.getByTestId('wo-date-warning')).toContainText('待核定')
 expect((await json('/api/work-orders/'+none.id)).planned_qty_base).toBe(mobileQty);expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true)
 await page.getByRole('button',{name:'採購管理',exact:false}).first().click();await page.getByRole('button',{name:'未到貨採購查詢',exact:true}).filter({visible:true}).click();await expect(page.getByTestId('oq-order-count')).toHaveText('2')
 expect(await json('/api/purchase-outstanding')).toEqual(fixtures.inquiry);expect(await json('/api/stock-positions')).toEqual(fixtures.stock)
 await page.getByRole('button',{name:'製造生產',exact:false}).first().click();await expect(page.getByRole('heading',{name:'產品 BOM 用料表',exact:true})).toBeVisible()
 await page.getByRole('button',{name:'製造工單管理',exact:true}).filter({visible:true}).click();await open(page,both);await expect(page.getByTestId('wo-bom-value')).toContainText('BOM-TEST-002');await expect(page.getByTestId('wo-routing-value')).toContainText('ROUTE-OTHER')
 for(const [resource,response] of Object.entries(fixtures.masterResponses))expect(await json('/api/'+resource)).toEqual(response)
 expect(legacy(snapshot())).toEqual(legacy(before));evidence.mobileCancelMutations=mutations;evidence.mobileExactQuantityAndDates=true;evidence.mobileNoOverflow=true;evidence.afterRestart={ownership:afterOwnership,orders:await json(),snapshot:snapshot()}
})
