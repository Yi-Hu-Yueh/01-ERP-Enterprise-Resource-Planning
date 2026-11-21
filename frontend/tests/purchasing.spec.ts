import { expectActionFeedback } from './feedback-assertions'
import { test, expect, type Page } from '@playwright/test'
import { spawn, execFileSync, type ChildProcess } from 'node:child_process'
import { mkdirSync, createWriteStream, writeFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { randomUUID } from 'node:crypto'

const root=path.resolve(fileURLToPath(new URL('../../',import.meta.url)))
const artifacts=path.resolve(root,process.env.ERP_TEST_ARTIFACTS||'artifacts/round3')
mkdirSync(artifacts,{recursive:true})
const dbPath=path.join(artifacts,'browser-purchase-'+randomUUID()+'.sqlite')
const backendUrl='http://127.0.0.1:8015'
const frontendUrl='http://127.0.0.1:5178'
let backend: ChildProcess, frontend: ChildProcess
const ownedProcesses: ChildProcess[]=[]
const evidence: Record<string,unknown>={database:dbPath,urls:{backend:backendUrl,frontend:frontendUrl},isolated:true}

function startBackend(){
  const py=path.join(root,'.venv',process.platform==='win32'?'Scripts/python.exe':'bin/python')
  backend=spawn(py,['-m','uvicorn','backend.app.main:app','--host','127.0.0.1','--port','8015'],{
    cwd:root,env:{...process.env,ERP_DB_PATH:dbPath},windowsHide:true,stdio:['ignore','pipe','pipe']})
  ownedProcesses.push(backend)
  backend.stdout?.pipe(createWriteStream(path.join(artifacts,'purchase-backend.log'),{flags:'a'}))
  backend.stderr?.pipe(createWriteStream(path.join(artifacts,'purchase-backend.log'),{flags:'a'}))
}
function startFrontend(){
  frontend=spawn(process.execPath,[path.join(root,'frontend','node_modules','vite','bin','vite.js'),'--host','127.0.0.1','--port','5178','--strictPort'],{
    cwd:path.join(root,'frontend'),env:{...process.env,ERP_API_TARGET:backendUrl},windowsHide:true,stdio:['ignore','pipe','pipe']})
  ownedProcesses.push(frontend)
  frontend.stdout?.pipe(createWriteStream(path.join(artifacts,'purchase-frontend.log'),{flags:'a'}))
  frontend.stderr?.pipe(createWriteStream(path.join(artifacts,'purchase-frontend.log'),{flags:'a'}))
}
async function waitReady(url:string){
  await expect.poll(async()=>{try{return (await fetch(url,{signal:AbortSignal.timeout(1500)})).status}catch{return 0}},{timeout:45_000}).toBe(200)
}
async function stop(child: ChildProcess){
  if(child.exitCode!==null)return
  if(process.platform==='win32'){
    try{execFileSync('taskkill',['/PID',String(child.pid),'/T','/F'],{windowsHide:true,stdio:'pipe'})}catch{if(child.exitCode===null)throw new Error('Could not stop owned process '+child.pid)}
  }else child.kill('SIGTERM')
  await new Promise<void>(resolve=>{if(child.exitCode!==null)resolve();else child.once('exit',()=>resolve())})
}



type Row=Record<string,any>
const url='/api/purchase-orders'
const suppliers:Row[]=[],materials:Row[]=[]
let currency:Row,unit:Row,warehouse:Row,finished:Row,offer:Row
let a:Row,b:Row
let saved:Row[]=[]
const headerBody=(r:Row)=>Object.fromEntries(['document_no','supplier_id','supplier_name_snapshot','currency_id','order_date','delivery_address_snapshot','payment_terms_snapshot','note','row_version'].map(k=>[k,r[k]]))
const lineBody=(r:Row,p:Row)=>({...Object.fromEntries(['item_id','description_snapshot','unit_id','ordered_qty','unit_price','due_date','preferred_warehouse_id','supplier_offer_id','row_version'].map(k=>[k,r[k]])),order_version:p.row_version})
const bTotal='1000000000000000000000000000000.125'+'0'.repeat(27)+'1'
test.use({baseURL:frontendUrl})
test.beforeAll(async({request})=>{
 for(const target of [backendUrl+'/api/health',frontendUrl]){let live=false;try{live=(await fetch(target,{signal:AbortSignal.timeout(1000)})).ok}catch{}if(live)throw new Error('QA port already occupied; nothing stopped: '+target)}
 startBackend();startFrontend();await waitReady(backendUrl+'/api/health');await waitReady(frontendUrl)
 async function create(resource:string,data:Row){const r=await request.post('/api/'+resource,{data});expect(r.status()).toBe(201);return r.json()}
 currency=await create('currencies',{code:'R7-CUR',name:'測試幣別'})
 unit=await create('units',{code:'R7-U',name:'測試單位'})
 const category=await create('categories',{code:'R7-CAT',name:'測試分類'})
 warehouse=await create('warehouses',{code:'R7-W',name:'測試收貨倉',is_active:true,mrp_included:false})
 for(const i of [1,2])suppliers.push(await create('suppliers',{code:'R7-S'+i,name:'測試供應商'+i,currency_id:currency.id,is_active:true,payment_terms:'原始付款條件'}))
 for(const i of [1,2])materials.push(await create('raw-materials',{code:'R7-R'+i,name:'測試原料'+i,specification:'原始規格',category_id:category.id,base_unit_id:unit.id,lot_tracking:false,is_active:true}))
 finished=await create('finished-goods',{code:'R7-FG',name:'不能採購的成品',category_id:category.id,base_unit_id:unit.id,lot_tracking:false,is_active:true})
 offer=await create('supplier-offers',{supplier_id:suppliers[0].id,item_id:materials[0].id,unit_price:'900',is_active:true})
 evidence.fixtures={suppliers,materials,finished,currency,unit,warehouse,offer}
})
test.afterAll(async()=>{for(const child of [...ownedProcesses].reverse())if(child.exitCode===null)await stop(child);writeFileSync(path.join(artifacts,'purchase-browser-evidence.json'),JSON.stringify(evidence,null,2)+'\n')})
async function fillHeader(page:Page,number:string,supplier=0){
 await page.locator('#po-document').fill(number);await page.locator('#po-date').fill('2026-11-19')
 await page.locator('#po-supplier').selectOption(suppliers[supplier].id)
 await page.locator('#po-delivery').fill('測試交貨地址');await page.locator('#po-payment').fill('測試付款條件');await page.locator('#po-note').fill('測試採購備註')
}
async function createHeader(page:Page,number:string,supplier=0){
 await page.getByRole('button',{name:'＋ 新增採購單',exact:true}).click();await fillHeader(page,number,supplier)
 await page.getByRole('button',{name:'儲存表頭',exact:true}).click();await expect(page.getByRole('status')).toHaveText('採購單已新增。')
 await expect(page.getByTestId('po-total')).toHaveText('0')
}
async function fillLine(page:Page,material:number,qty:string,price:string,reference=false){
 await page.locator('#po-material').selectOption(materials[material].id)
 await page.locator('#po-qty').fill(qty);await page.locator('#po-price').fill(price)
 await page.locator('#po-due').fill('2026-11-20');await page.locator('#po-warehouse').selectOption(warehouse.id)
 if(reference){await page.locator('#po-offer').selectOption(offer.id);await page.getByRole('button',{name:'帶入參考單價',exact:true}).click();await expect(page.locator('#po-price')).toHaveValue('900');await page.locator('#po-price').fill(price)}
}
async function addLine(page:Page,material:number,qty:string,price:string,reference=false){
 await page.getByRole('button',{name:'＋ 新增明細',exact:true}).click();await fillLine(page,material,qty,price,reference)
 await page.getByRole('button',{name:'儲存明細',exact:true}).click();await expect(page.locator('.po-lines').getByRole('status')).toContainText('明細已新增。')
}
async function read(request:any,row:Row){return(await request.get(url+'/'+row.id)).json()}
async function assertUI(page:Page,row:Row){
 await expect(page.getByTestId('po-total')).toHaveText(row.total_amount)
 await expect(page.getByTestId('po-order-date')).toHaveText(row.order_date)
 await expect(page.getByTestId('po-note-value')).toHaveText(row.note||'—')
 for(const l of row.lines){const article=page.getByTestId('po-line-'+l.line_no);await expect(article).toContainText(l.description_snapshot);await expect(article).toContainText(l.ordered_qty+' × '+l.unit_price);await expect(page.getByTestId('po-amount-'+l.line_no)).toHaveText(l.line_amount)}
}

test('採購表頭明細CRUD、300→325→150、各單隔離、取消與失敗保護',async({page,request})=>{
 test.setTimeout(300_000)
 await page.goto('/')
 await page.getByRole('button',{name:/採購管理/}).click()
 await expect(page.getByRole('heading',{name:'採購訂單管理',exact:true})).toBeVisible()
 await expect(page.locator('#po-date')).toHaveAttribute('type','date')
 await createHeader(page,'R7-A')
 a=(await(await request.get(url)).json()).find((r:Row)=>r.document_no==='R7-A')
 await addLine(page,0,'10','12.5',true);await expect(page.getByTestId('po-total')).toHaveText('125')
 await addLine(page,1,'20','8.75');await expect(page.getByTestId('po-total')).toHaveText('300')
 evidence.baseline={line1:'125',line2:'175',total:'300'}
 await createHeader(page,'R7-B',1)
 b=(await(await request.get(url)).json()).find((r:Row)=>r.document_no==='R7-B')
 await addLine(page,1,'7','-0.125');await expect(page.getByTestId('po-total')).toHaveText('-0.875')
 await addLine(page,1,'0','-99');await expect(page.getByTestId('po-total')).toHaveText('-0.875')
 await addLine(page,0,'1.000000000000000000000000000001','1000000000000000000000000000000')
 await expect(page.getByTestId('po-total')).toHaveText('1000000000000000000000000000000.125')
 await addLine(page,0,'0.000000000000000000000000000001','0.1');await expect(page.getByTestId('po-total')).toHaveText(bTotal)
 const bBefore=await read(request,b)
 await page.getByRole('button',{name:'開啟採購單 R7-A',exact:true}).click();await expect(page.getByTestId('po-total')).toHaveText('300')
 const mutations:string[]=[];page.on('request',r=>{if(['POST','PUT','PATCH','DELETE'].includes(r.method()))mutations.push(r.method()+' '+r.url())})
 const beforeHeader=await read(request,a)
 await page.getByRole('button',{name:'編輯表頭',exact:true}).click();await page.locator('#po-note').fill('取消不得保存')
 let count=mutations.length
 await page.locator('.po-header').getByRole('button',{name:'取消',exact:true}).click();await page.waitForLoadState('networkidle')
 expect(mutations.length).toBe(count);expect(await read(request,a)).toEqual(beforeHeader)
 evidence.cancelHeader={mutationRequests:0,storedRowsUnchanged:true}
 await page.getByRole('button',{name:'編輯明細 1',exact:true}).click();await page.locator('#po-qty').fill('999');await page.locator('#po-price').fill('999')
 count=mutations.length
 await page.getByRole('form',{name:'編輯採購明細'}).getByRole('button',{name:'取消',exact:true}).click();await page.waitForLoadState('networkidle')
 expect(mutations.length).toBe(count);expect(await read(request,a)).toEqual(beforeHeader)
 evidence.cancelLine={mutationRequests:0,storedRowsUnchanged:true}
 await page.getByRole('button',{name:'＋ 新增明細',exact:true}).click();await fillLine(page,0,'NaN','1')
 await expect(page.locator('#po-material option[value="'+finished.id+'"]')).toHaveCount(0)
 await page.getByRole('button',{name:'儲存明細',exact:true}).click();await expect(page.getByRole('alert')).toContainText('訂購數量')
 await expectActionFeedback(page,'.po-line-editor .form-actions');expect(await read(request,a)).toEqual(beforeHeader)
 await page.getByRole('form',{name:'新增採購明細'}).getByRole('button',{name:'取消',exact:true}).click()
 await page.getByRole('button',{name:'編輯明細 1',exact:true}).click();await page.locator('#po-price').fill('13')
 await page.getByRole('button',{name:'儲存明細',exact:true}).click();await expect(page.getByTestId('po-total')).toHaveText('305');expect(await read(request,b)).toEqual(bBefore)
 await page.getByRole('button',{name:'編輯明細 1',exact:true}).click();await page.locator('#po-price').fill('12.5')
 await page.getByRole('button',{name:'儲存明細',exact:true}).click();await expect(page.getByTestId('po-total')).toHaveText('300')
 await page.getByRole('button',{name:'編輯明細 1',exact:true}).click();await page.locator('#po-qty').fill('12')
 await page.getByRole('button',{name:'儲存明細',exact:true}).click();await expect(page.getByTestId('po-amount-1')).toHaveText('150');await expect(page.getByTestId('po-total')).toHaveText('325');expect(await read(request,b)).toEqual(bBefore)
 page.once('dialog',d=>d.accept());await page.getByRole('button',{name:'刪除明細 2',exact:true}).click();await expect(page.getByTestId('po-total')).toHaveText('150');expect(await read(request,b)).toEqual(bBefore)
 evidence.quantityUpdate={amount:'150',total:'325'};evidence.lineDelete={total:'150',otherOrderUnchanged:true}
 await page.getByRole('button',{name:'編輯表頭',exact:true}).click();await page.locator('#po-document').fill('R7-A-EDIT');await page.locator('#po-date').fill('2026-11-18');await page.locator('#po-note').fill('表頭修訂已保存')
 await page.getByRole('button',{name:'儲存表頭',exact:true}).click();await expect(page.getByRole('status')).toHaveText('表頭變更已儲存。')
 let current=await read(request,a)
 expect(current.id).toBe(a.id);expect(current.created_at).toBe(a.created_at);expect(current.approval_state).toBeNull()
 page.once('dialog',d=>d.accept());await page.getByRole('button',{name:'刪除採購單',exact:true}).click();await expect(page.getByRole('alert')).toContainText('仍有明細');await expectActionFeedback(page,'.po-header .form-actions');expect(await read(request,a)).toEqual(current)
 await page.getByRole('button',{name:'編輯表頭',exact:true}).click()
 const newer=await request.put(url+'/'+a.id,{data:{...headerBody(current),note:'另一端修改的備註'}});expect(newer.status()).toBe(200)
 await page.locator('#po-note').fill('過期表頭不得覆蓋');await page.getByRole('button',{name:'儲存表頭',exact:true}).click();await expect(page.getByRole('alert')).toContainText('另一個視窗更新');await expectActionFeedback(page,'.po-header .form-actions')
 expect(await read(request,a)).toEqual(await newer.json());await page.getByRole('button',{name:'重新讀取',exact:true}).click()
 current=await read(request,a)
 await page.getByRole('button',{name:'編輯明細 1',exact:true}).click()
 const external=await request.put(url+'/'+a.id+'/lines/'+current.lines[0].id,{data:{...lineBody(current.lines[0],current),description_snapshot:'另一端修訂品名快照'}});expect(external.status()).toBe(200)
 await page.locator('#po-qty').fill('13');await page.getByRole('button',{name:'儲存明細',exact:true}).click();await expect(page.getByRole('alert')).toContainText('另一個視窗更新');await expectActionFeedback(page,'.po-line-editor .form-actions')
 expect((await read(request,a)).total_amount).toBe('150');await page.getByRole('button',{name:'重新讀取',exact:true}).click()
 expect((await request.put('/api/supplier-offers/'+offer.id,{data:{supplier_id:offer.supplier_id,item_id:offer.item_id,unit_price:'901',is_active:true,row_version:1}})).status()).toBe(200)
 expect((await read(request,a)).lines[0].unit_price).toBe('12.5')
 await page.getByRole('button',{name:'＋ 新增採購單',exact:true}).click();await fillHeader(page,'R7-A-EDIT');await page.getByRole('button',{name:'儲存表頭',exact:true}).click();await expect(page.getByRole('alert')).toContainText('單號已存在');await expectActionFeedback(page,'.po-header .form-actions')
 expect((await(await request.get(url)).json())).toHaveLength(2)
 await page.locator('.po-header').getByRole('button',{name:'取消',exact:true}).click()
 await page.getByLabel('搜尋採購單',{exact:true}).fill('R7-A-EDIT');await page.getByRole('button',{name:'搜尋',exact:true}).click();await expect(page.getByRole('button',{name:'開啟採購單 R7-A-EDIT',exact:true})).toBeVisible();await expect(page.getByRole('button',{name:'開啟採購單 R7-B',exact:true})).not.toBeVisible()
 await page.getByLabel('搜尋採購單',{exact:true}).fill('');await page.getByRole('button',{name:'搜尋',exact:true}).click()
 saved=[await read(request,a),await read(request,b)]
 for(const row of saved){await page.getByRole('button',{name:'開啟採購單 '+row.document_no,exact:true}).click();await assertUI(page,row)}
 await page.reload();await expect(page.getByRole('heading',{name:'採購訂單管理',exact:true})).toBeVisible()
 for(const row of saved){await page.getByRole('button',{name:'開啟採購單 '+row.document_no,exact:true}).click();await assertUI(page,row);expect(await read(request,row)).toEqual(row)}
 evidence.f5={realDocumentReload:true,method:'Actual page.reload(), F5-equivalent'};evidence.otherOrderUnaffected=true;evidence.offerChangesDoNotRewritePrices=true
})

test('採購單真實前後端完整停止及重啟、SQLite/API/UI資料與SUM一致',async({page,request})=>{
 expect(saved).toHaveLength(2)
 const before={backend:backend.pid,frontend:frontend.pid};await stop(frontend);await stop(backend)
 for(const target of [backendUrl+'/api/health',frontendUrl])await expect.poll(async()=>{try{await fetch(target,{signal:AbortSignal.timeout(1000)});return true}catch{return false}}).toBe(false)
 startBackend();startFrontend();await waitReady(backendUrl+'/api/health');await waitReady(frontendUrl)
 expect(backend.pid).not.toBe(before.backend);expect(frontend.pid).not.toBe(before.frontend)
 await page.goto('/#purchase-orders')
 for(const row of saved){await page.getByRole('button',{name:'開啟採購單 '+row.document_no,exact:true}).click();await assertUI(page,row);expect(await read(request,row)).toEqual(row)}
 await page.getByRole('button',{name:'開啟採購單 R7-A-EDIT',exact:true}).click();await assertUI(page,saved[0]);await expect(page.locator('.po-line')).toHaveCount(1);await page.evaluate(()=>window.scrollTo(0,0));await page.screenshot({path:path.join(artifacts,'purchase-order-desktop.png'),fullPage:true})
 const listeners=JSON.parse(execFileSync('powershell.exe',['-NoProfile','-Command','Get-NetTCPConnection -State Listen -LocalPort 8015,5178 | Select-Object LocalAddress,LocalPort,OwningProcess | ConvertTo-Json'],{windowsHide:true,encoding:'utf8'}))
 expect(listeners).toHaveLength(2);expect(listeners.every((r:{LocalAddress:string})=>r.LocalAddress==='127.0.0.1')).toBe(true)
 expect((await request.get(frontendUrl,{headers:{Host:'attacker.invalid'}})).status()).toBe(403)
 expect((await request.get(frontendUrl,{headers:{Origin:'https://untrusted.invalid'}})).headers()['access-control-allow-origin']).toBeUndefined()
 const health=await(await request.get('/api/health')).json();expect(health.foreign_keys).toBe(1)
 evidence.restart={fullyStopped:true,offlineVerified:true,before,after:{backend:backend.pid,frontend:frontend.pid},persistence:true}
 evidence.persistedOrders=saved;evidence.loopbackListeners=listeners;evidence.health=health
})

test('手機採購表頭與單筆明細CRUD、取消零寫入、空單刪除',async({page,request})=>{
 await page.setViewportSize({width:390,height:844});await page.goto('/#purchase-orders')
 await createHeader(page,'R7-MOBILE')
 const mobile=(await(await request.get(url)).json()).find((r:Row)=>r.document_no==='R7-MOBILE')
 await addLine(page,1,'-2.5','0.1');await expect(page.getByTestId('po-total')).toHaveText('-0.25')
 const current=await read(request,mobile)
 const mutations:string[]=[];page.on('request',r=>{if(['POST','PUT','PATCH','DELETE'].includes(r.method()))mutations.push(r.method())})
 await page.getByRole('button',{name:'編輯表頭',exact:true}).click();await page.locator('#po-note').fill('手機取消表頭')
 let count=mutations.length;await page.locator('.po-header').getByRole('button',{name:'取消',exact:true}).click();await page.waitForLoadState('networkidle');expect(mutations.length).toBe(count);expect(await read(request,mobile)).toEqual(current)
 await page.getByRole('button',{name:'編輯明細 1',exact:true}).click();await page.locator('#po-qty').fill('999');count=mutations.length
 await page.getByRole('form',{name:'編輯採購明細'}).getByRole('button',{name:'取消',exact:true}).click();await page.waitForLoadState('networkidle');expect(mutations.length).toBe(count);expect(await read(request,mobile)).toEqual(current)
 await page.getByRole('button',{name:'編輯明細 1',exact:true}).click();await page.locator('#po-price').fill('-0.2');await page.getByRole('button',{name:'儲存明細',exact:true}).click();await expect(page.getByTestId('po-total')).toHaveText('0.5')
 page.once('dialog',d=>d.accept());await page.getByRole('button',{name:'刪除採購單',exact:true}).click();await expect(page.getByRole('alert')).toContainText('仍有明細');await expectActionFeedback(page,'.po-header .form-actions')
 await page.screenshot({path:path.join(artifacts,'purchase-order-mobile.png'),fullPage:true})
 expect(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth)).toBe(true)
 page.once('dialog',d=>d.accept());await page.getByRole('button',{name:'刪除明細 1',exact:true}).click();await expect(page.getByTestId('po-total')).toHaveText('0')
 page.once('dialog',d=>d.accept());await page.getByRole('button',{name:'刪除採購單',exact:true}).click();await expect(page.locator('.po-header').getByRole('status')).toHaveText('採購單已刪除。')
 expect((await request.get(url+'/'+mobile.id)).status()).toBe(404)
 expect(await read(request,saved[0])).toEqual(saved[0]);expect(await read(request,saved[1])).toEqual(saved[1])
 evidence.mobile={headerCrud:true,lineCrud:true,headerCancelMutationRequests:0,lineCancelMutationRequests:0,noHorizontalOverflow:true,emptyHeaderDelete:true}
})
