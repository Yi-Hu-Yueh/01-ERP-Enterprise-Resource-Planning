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
const dbPath=path.join(artifacts,'browser-offer-'+randomUUID()+'.sqlite')
const backendUrl='http://127.0.0.1:8014'
const frontendUrl='http://127.0.0.1:5177'
let backend: ChildProcess, frontend: ChildProcess
const ownedProcesses: ChildProcess[]=[]
const evidence: Record<string,unknown>={database:dbPath,urls:{backend:backendUrl,frontend:frontendUrl},isolated:true}

function startBackend(){
  const py=path.join(root,'.venv',process.platform==='win32'?'Scripts/python.exe':'bin/python')
  backend=spawn(py,['-m','uvicorn','backend.app.main:app','--host','127.0.0.1','--port','8014'],{
    cwd:root,env:{...process.env,ERP_DB_PATH:dbPath},windowsHide:true,stdio:['ignore','pipe','pipe']})
  ownedProcesses.push(backend)
  backend.stdout?.pipe(createWriteStream(path.join(artifacts,'offer-backend.log'),{flags:'a'}))
  backend.stderr?.pipe(createWriteStream(path.join(artifacts,'offer-backend.log'),{flags:'a'}))
}
function startFrontend(){
  frontend=spawn(process.execPath,[path.join(root,'frontend','node_modules','vite','bin','vite.js'),'--host','127.0.0.1','--port','5177','--strictPort'],{
    cwd:path.join(root,'frontend'),env:{...process.env,ERP_API_TARGET:backendUrl},windowsHide:true,stdio:['ignore','pipe','pipe']})
  ownedProcesses.push(frontend)
  frontend.stdout?.pipe(createWriteStream(path.join(artifacts,'offer-frontend.log'),{flags:'a'}))
  frontend.stderr?.pipe(createWriteStream(path.join(artifacts,'offer-frontend.log'),{flags:'a'}))
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
const suppliers:Row[]=[],materials:Row[]=[]
let finished:Row
let persisted:Row[]=[]
const url='/api/supplier-offers'
const exact='-1234567890123456789012345678901234567890.12345678901234567890123456789'
const body=(r:Row)=>Object.fromEntries(['supplier_id','item_id','unit_price','is_active','row_version'].map(k=>[k,r[k]]))
const label=(r:Row)=>'開啟 '+r.supplier_code+' / '+r.item_code
const pair=(s:Row,r:Row)=>({supplier_id:s.id,item_id:r.id,is_active:true})
test.use({baseURL:frontendUrl})
test.beforeAll(async({request})=>{
  for(const target of [backendUrl+'/api/health',frontendUrl]){
    let live=false;try{live=(await fetch(target,{signal:AbortSignal.timeout(1500)})).ok}catch{}
    if(live)throw new Error('QA service already occupied; no process stopped: '+target)
  }
  startBackend();startFrontend();await waitReady(backendUrl+'/api/health');await waitReady(frontendUrl)
  async function create(resource:string,data:Row){const response=await request.post('/api/'+resource,{data});expect(response.status()).toBe(201);return response.json()}
  const currency=await create('currencies',{code:'R6-CUR',name:'測試幣別'})
  const category=await create('categories',{code:'R6-CAT',name:'測試分類'})
  const unit=await create('units',{code:'R6-U',name:'測試單位'})
  for(const i of [1,2])suppliers.push(await create('suppliers',{code:'R6-S'+i,name:'測試供應商'+i,currency_id:currency.id,is_active:true}))
  for(const i of [1,2])materials.push(await create('raw-materials',{code:'R6-R'+i,name:'測試原料'+i,category_id:category.id,base_unit_id:unit.id,lot_tracking:false,is_active:true}))
  finished=await create('finished-goods',{code:'R6-FG',name:'不能供料的成品',category_id:category.id,base_unit_id:unit.id,lot_tracking:false,is_active:true})
  evidence.suppliers=suppliers;evidence.materials=materials;evidence.finished=finished
})
test.afterAll(async()=>{
  for(const child of [...ownedProcesses].reverse())if(child.exitCode===null)await stop(child)
  writeFileSync(path.join(artifacts,'supplier-offers-browser-evidence.json'),JSON.stringify(evidence,null,2)+'\n')
})
async function fill(page:Page,s:Row,r:Row,price:string){
  await page.locator('#offer-supplier').selectOption(s.id)
  await page.locator('#offer-material').selectOption(r.id)
  await page.locator('#offer-price').fill(price)
}
async function verify(page:Page,row:Row){
  await expect(page.locator('#offer-supplier')).toHaveValue(row.supplier_id)
  await expect(page.locator('#offer-material')).toHaveValue(row.item_id)
  await expect(page.locator('#offer-price')).toHaveValue(row.unit_price)
  await expect(page.getByRole('checkbox',{name:'啟用此供料設定',exact:true})).toBeChecked({checked:row.is_active})
}

async function protectedSupplierFeedback(page:Page,request:any,mobile=false){
  await page.setViewportSize(mobile?{width:390,height:844}:{width:1280,height:720})
  await page.getByRole('button',{name:'供應商資料維護',exact:true}).click()
  await page.getByRole('button',{name:'開啟 '+suppliers[0].code,exact:true}).click()
  const before=await(await request.get('/api/suppliers/'+suppliers[0].id)).json()
  page.once('dialog',d=>d.accept())
  await page.getByRole('button',{name:'刪除供應商',exact:true}).click()
  await expect(page.getByRole('alert')).toContainText('引用')
  await expectActionFeedback(page)
  expect(await(await request.get('/api/suppliers/'+suppliers[0].id)).json()).toEqual(before)
  await page.screenshot({path:path.join(artifacts,'supplier-delete-feedback-'+(mobile?'mobile':'desktop')+'.png'),fullPage:true})
  evidence[mobile?'supplierFeedbackMobile':'supplierFeedbackDesktop']={nearActionButtons:true,alertAndButtonsInViewport:true,focused:true,recordUnchanged:true}
  await page.getByRole('button',{name:'供應商供料設定',exact:true}).click()
  if(!mobile)await page.setViewportSize({width:1440,height:1100})
}

test('供料多對多完整矩陣、精確價格、重複保護、重新指派、刪除、取消及F5',async({page,request})=>{
  test.setTimeout(240_000)
  await page.goto('/')
  await page.getByRole('button',{name:'供應商供料設定',exact:true}).click()
  await expect(page.getByRole('heading',{name:'供應商供料設定',exact:true})).toBeVisible()
  await expect(page.locator('#offer-material option')).toHaveCount(3)
  await expect(page.locator('#offer-material option[value="'+finished.id+'"]')).toHaveCount(0)
  const prices=['0','-2.125','0.000000000000000000000000000001','123456789012345678901234567890.1234567890123456789']
  let index=0
  for(const s of suppliers)for(const r of materials){
    await page.getByRole('button',{name:'新增供料設定',exact:true}).click()
    await fill(page,s,r,prices[index++])
    await page.getByRole('button',{name:'儲存供料設定',exact:true}).click()
    await expect(page.getByRole('status')).toHaveText('供料設定已新增。')
  }
  const matrix=await (await request.get(url)).json() as Row[]
  expect(matrix).toHaveLength(4)
  expect(new Set(matrix.map(r=>r.supplier_id+'|'+r.item_id))).toEqual(new Set(suppliers.flatMap(s=>materials.map(r=>s.id+'|'+r.id))))
  for(const [i,row]of matrix.entries())expect(row.unit_price).toBe(prices[i])
  evidence.initialMatrix=matrix
  await protectedSupplierFeedback(page,request)
  const mutations:string[]=[]
  page.on('request',r=>{if(['POST','PUT','PATCH','DELETE'].includes(r.method()))mutations.push(r.method()+' '+r.url())})
  await page.getByRole('button',{name:label(matrix[0]),exact:true}).click()
  await fill(page,suppliers[1],materials[1],'999999.99')
  await page.getByRole('checkbox',{name:'啟用此供料設定',exact:true}).uncheck()
  const before=mutations.length
  await page.locator('.editor-panel').getByRole('button',{name:'取消',exact:true}).click()
  await expect(page.getByRole('heading',{name:'新增供料設定',exact:true})).toBeVisible()
  await expect(page.locator('#offer-price')).toHaveValue('')
  await page.waitForLoadState('networkidle')
  expect(mutations.length).toBe(before)
  expect(await (await request.get(url)).json()).toEqual(matrix)
  evidence.cancelDesktop={mutationRequests:mutations.length-before,committedRowsUnchanged:true,exitedEditMode:true}
  await fill(page,suppliers[0],materials[0],'5')
  await page.getByRole('button',{name:'儲存供料設定',exact:true}).click()
  await expect(page.getByRole('alert')).toContainText('已有啟用')
    await expectActionFeedback(page)
  await page.getByRole('button',{name:label(matrix[3]),exact:true}).click()
  await fill(page,suppliers[0],materials[0],'7')
  await page.getByRole('button',{name:'儲存變更',exact:true}).click()
  await expect(page.getByRole('alert')).toContainText('已有啟用')
    await expectActionFeedback(page)
  expect(await (await request.get(url)).json()).toEqual(matrix)
  for(const patch of [{supplier_id:randomUUID()},{item_id:randomUUID()},{item_id:finished.id},{unit_price:0.1},{unit_price:'NaN'}]){
    expect((await request.put(url+'/'+matrix[0].id,{data:{...body(matrix[0]),...patch}})).status()).toBe(422)
  }
  for(const [resource,row]of [['suppliers',suppliers[0]],['raw-materials',materials[0]]] as const){
    const response=await request.delete('/api/'+resource+'/'+row.id+'?row_version='+row.row_version)
    expect(response.status()).toBe(409);expect((await response.json()).message).toContain('引用')
  }
  await page.getByRole('button',{name:label(matrix[3]),exact:true}).click()
  page.once('dialog',d=>d.accept())
  await page.getByRole('button',{name:'刪除供料設定',exact:true}).click()
  await expect(page.getByRole('status')).toHaveText('供料設定已刪除。')
  expect(await (await request.get('/api/suppliers')).json()).toEqual(suppliers)
  expect(await (await request.get('/api/raw-materials')).json()).toEqual(materials)
  expect(await (await request.get('/api/finished-goods')).json()).toEqual([finished])
  await page.getByRole('button',{name:label(matrix[1]),exact:true}).click()
  await fill(page,suppliers[1],materials[1],'-0.000007')
  await page.getByRole('button',{name:'儲存變更',exact:true}).click()
  await expect(page.getByRole('status')).toHaveText('變更已儲存。')
  const reassigned=await (await request.get(url+'/'+matrix[1].id)).json()
  expect(reassigned.supplier_id).toBe(suppliers[1].id);expect(reassigned.item_id).toBe(materials[1].id)
  expect(reassigned.id).toBe(matrix[1].id);expect(reassigned.row_version).toBe(2)
  await page.getByRole('button',{name:'新增供料設定',exact:true}).click()
  await fill(page,suppliers[0],materials[1],'1.00000000000000000000000001')
  await page.getByRole('button',{name:'儲存供料設定',exact:true}).click()
  await expect(page.getByRole('status')).toHaveText('供料設定已新增。')
  await page.getByRole('button',{name:label(matrix[0]),exact:true}).click()
  await page.locator('#offer-price').fill(exact)
  await page.getByRole('button',{name:'儲存變更',exact:true}).click()
  await expect(page.getByRole('status')).toHaveText('變更已儲存。')
  const first=await (await request.get(url+'/'+matrix[0].id)).json()
  expect(first.unit_price).toBe(exact)
  const newer=await request.put(url+'/'+first.id,{data:{...body(first),unit_price:'-0.125'}})
  expect(newer.status()).toBe(200)
  await page.locator('#offer-price').fill('123')
  await page.getByRole('button',{name:'儲存變更',exact:true}).click()
  await expect(page.getByRole('alert')).toContainText('另一個視窗更新')
    await expectActionFeedback(page)
  await page.getByRole('button',{name:'重新讀取',exact:true}).click()
  await expect(page.locator('#offer-price')).toHaveValue('-0.125')
  await page.locator('#offer-price').fill(exact)
  await page.getByRole('button',{name:'儲存變更',exact:true}).click()
  await expect(page.getByRole('status')).toHaveText('變更已儲存。')
  for(const q of ['R6-S1','測試供應商1','R6-R1','測試原料1']){
    await page.getByLabel('搜尋供應商或原料',{exact:true}).fill(q)
    await page.getByRole('button',{name:'搜尋',exact:true}).click()
    await expect(page.locator('.offer-table tbody tr')).toHaveCount(2)
  }
  await page.getByLabel('搜尋供應商或原料',{exact:true}).fill('')
  await page.getByRole('button',{name:'搜尋',exact:true}).click()
  persisted=await (await request.get(url)).json()
  await page.reload()
  await expect(page.getByRole('heading',{name:'供應商供料設定',exact:true})).toBeVisible()
  for(const row of persisted){await page.getByRole('button',{name:label(row),exact:true}).click();await verify(page,row)}
  expect(await (await request.get(url)).json()).toEqual(persisted)
  evidence.realDocumentRefresh=true;evidence.refreshMethod='Actual page.reload(), F5-equivalent document reload'
  evidence.reassignment=reassigned;evidence.masterRecordsUnchanged=true
})

test('供料前後端確實停止與不同程序重啟、關聯及所有價格持續保存',async({page,request})=>{
  expect(persisted).toHaveLength(4)
  const before={backend:backend.pid,frontend:frontend.pid}
  await stop(frontend);await stop(backend)
  for(const target of [backendUrl+'/api/health',frontendUrl])await expect.poll(async()=>{try{await fetch(target,{signal:AbortSignal.timeout(1000)});return true}catch{return false}}).toBe(false)
  startBackend();startFrontend();await waitReady(backendUrl+'/api/health');await waitReady(frontendUrl)
  expect(backend.pid).not.toBe(before.backend);expect(frontend.pid).not.toBe(before.frontend)
  await page.goto('/#supplier-offers')
  for(const row of persisted){
    await page.getByRole('button',{name:label(row),exact:true}).click();await verify(page,row)
    expect(await (await request.get(url+'/'+row.id)).json()).toEqual(row)
  }
  await page.getByRole('button',{name:label(persisted[0]),exact:true}).click()
  await page.locator('details.audit summary').click();await expect(page.getByTestId('offer-uuid')).toHaveText(persisted[0].id)
  await page.evaluate(()=>window.scrollTo(0,0))
  await page.screenshot({path:path.join(artifacts,'supplier-offers-desktop.png'),fullPage:true})
  const listeners=JSON.parse(execFileSync('powershell.exe',['-NoProfile','-Command','Get-NetTCPConnection -State Listen -LocalPort 8014,5177 | Select-Object LocalAddress,LocalPort,OwningProcess | ConvertTo-Json'],{windowsHide:true,encoding:'utf8'}))
  expect(listeners).toHaveLength(2);expect(listeners.every((r:{LocalAddress:string})=>r.LocalAddress==='127.0.0.1')).toBe(true)
  expect((await request.get(frontendUrl,{headers:{Host:'attacker.invalid'}})).status()).toBe(403)
  expect((await request.get(frontendUrl,{headers:{Origin:'https://untrusted.invalid'}})).headers()['access-control-allow-origin']).toBeUndefined()
  const health=await(await request.get('/api/health')).json();expect(health.foreign_keys).toBe(1)
  evidence.persistedRecords=persisted;evidence.beforePids=before;evidence.afterPids={backend:backend.pid,frontend:frontend.pid}
  evidence.fullStopAndRestart=true;evidence.loopbackListeners=listeners;evidence.health=health
})

test('手機實際供料新增編輯刪除、取消零寫入、無橫向溢位',async({page,request})=>{
  await page.setViewportSize({width:390,height:844});await page.goto('/')
  await page.getByRole('button',{name:'供應商供料設定',exact:true}).click()
  await protectedSupplierFeedback(page,request,true)
  await fill(page,suppliers[0],materials[0],'0.123456789012345678901234567890123456789')
  await page.getByRole('checkbox',{name:'啟用此供料設定',exact:true}).uncheck()
  await page.getByRole('button',{name:'儲存供料設定',exact:true}).click()
  await expect(page.getByRole('status')).toHaveText('供料設定已新增。')
  const row=(await(await request.get(url)).json()).find((r:Row)=>!r.is_active)
  await page.locator('#offer-price').fill('-0.000000000000000000000000009')
  await page.getByRole('button',{name:'儲存變更',exact:true}).click()
  await expect(page.getByRole('status')).toHaveText('變更已儲存。')
  const before=await(await request.get(url+'/'+row.id)).json()
  const mutations:string[]=[];page.on('request',r=>{if(['POST','PUT','PATCH','DELETE'].includes(r.method()))mutations.push(r.method())})
  await fill(page,suppliers[1],materials[1],'999')
  await page.getByRole('checkbox',{name:'啟用此供料設定',exact:true}).check()
  await page.locator('.editor-panel').getByRole('button',{name:'取消',exact:true}).click()
  await expect(page.getByRole('heading',{name:'新增供料設定',exact:true})).toBeVisible()
  await page.waitForLoadState('networkidle');expect(mutations).toEqual([])
  expect(await(await request.get(url+'/'+row.id)).json()).toEqual(before)
  // Active/inactive rows may share a pair; open the exact persisted UUID via its row.
  const inactiveRow=page.locator('.offer-table tbody tr').filter({has:page.getByText('停用',{exact:true})})
  await inactiveRow.getByRole('button').click();await verify(page,before)
  page.once('dialog',d=>d.accept());await page.getByRole('button',{name:'刪除供料設定',exact:true}).click()
  await expect(page.getByRole('status')).toHaveText('供料設定已刪除。')
  expect((await request.get(url+'/'+row.id)).status()).toBe(404)
  expect(await(await request.get('/api/suppliers')).json()).toEqual(suppliers)
  expect(await(await request.get('/api/raw-materials')).json()).toEqual(materials)
  await page.getByRole('button',{name:label(persisted[0]),exact:true}).click();await verify(page,persisted[0])
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth)).toBe(true)
  await page.screenshot({path:path.join(artifacts,'supplier-offers-mobile.png'),fullPage:true})
  evidence.mobile={crud:true,cancelMutationRequests:0,storedRecordUnchanged:true,noHorizontalOverflow:true}
})
