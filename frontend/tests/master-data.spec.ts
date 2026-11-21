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
const dbPath=path.join(artifacts,'browser-master-'+randomUUID()+'.sqlite')
const backendUrl='http://127.0.0.1:8013'
const frontendUrl='http://127.0.0.1:5176'
let backend: ChildProcess, frontend: ChildProcess
const ownedProcesses: ChildProcess[]=[]
const evidence: Record<string,unknown>={database:dbPath,urls:{backend:backendUrl,frontend:frontendUrl},isolated:true}

function startBackend(){
  const py=path.join(root,'.venv',process.platform==='win32'?'Scripts/python.exe':'bin/python')
  backend=spawn(py,['-m','uvicorn','backend.app.main:app','--host','127.0.0.1','--port','8013'],{
    cwd:root,env:{...process.env,ERP_DB_PATH:dbPath},windowsHide:true,stdio:['ignore','pipe','pipe']})
  ownedProcesses.push(backend)
  backend.stdout?.pipe(createWriteStream(path.join(artifacts,'master-backend.log'),{flags:'a'}))
  backend.stderr?.pipe(createWriteStream(path.join(artifacts,'master-backend.log'),{flags:'a'}))
}
function startFrontend(){
  frontend=spawn(process.execPath,[path.join(root,'frontend','node_modules','vite','bin','vite.js'),'--host','127.0.0.1','--port','5176','--strictPort'],{
    cwd:path.join(root,'frontend'),env:{...process.env,ERP_API_TARGET:backendUrl},windowsHide:true,stdio:['ignore','pipe','pipe']})
  ownedProcesses.push(frontend)
  frontend.stdout?.pipe(createWriteStream(path.join(artifacts,'master-frontend.log'),{flags:'a'}))
  frontend.stderr?.pipe(createWriteStream(path.join(artifacts,'master-frontend.log'),{flags:'a'}))
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

const operations=[
  {resource:'customers',title:'客戶資料維護',noun:'客戶',fields:{tax_identifier:'TAX-TEST',contact_name:'林窗口',phone:'+886 (02) 001-002',email:'buyer@example.com',payment_terms:'月底另議',billing_address:'臺北帳單地址',default_ship_address:'臺中送貨地址'}},
  {resource:'suppliers',title:'供應商資料維護',noun:'供應商',fields:{tax_identifier:'TAX-TEST',contact_name:'林窗口',phone:'+886 (02) 001-002',email:'seller@example.com',payment_terms:'月底另議',address:'臺北供應地址'}},
  {resource:'warehouses',title:'倉庫資料維護',noun:'倉庫',fields:{address:'北區位置',mrp_included:true}},
  {resource:'work-centers',title:'工作中心維護',noun:'工作中心',fields:{description:'裁切區說明'}},
]
type Row=Record<string,any>
const persisted:Record<string,Row>={}
let currencyId=''
test.use({baseURL:frontendUrl})
test.beforeAll(async()=>{
  for(const url of [backendUrl+'/api/health',frontendUrl]){
    let live=false;try{live=(await fetch(url,{signal:AbortSignal.timeout(1500)})).ok}catch{}
    if(live)throw new Error('QA service already occupied; no process stopped: '+url)
  }
  startBackend();startFrontend();await waitReady(backendUrl+'/api/health');await waitReady(frontendUrl)
})
test.afterAll(async()=>{
  for(const child of [...ownedProcesses].reverse())if(child.exitCode===null)await stop(child)
  writeFileSync(path.join(artifacts,'master-data-browser-evidence.json'),JSON.stringify(evidence,null,2)+'\n')
})
function body(row:Row){
  return Object.fromEntries(Object.entries(row).filter(([key])=>!['id','created_at','created_by','updated_at'].includes(key)))
}
async function fill(page:Page,operation:typeof operations[number],code:string,name:string,edited=false){
  await page.locator('#master-code').fill(code);await page.locator('#master-name').fill(name)
  for(const [key,original]of Object.entries(operation.fields)){
    const value=key==='email'?(edited?'updated@example.com':String(original)):edited&&typeof original==='string'?'修訂 '+original:original
    if(typeof original==='boolean')await page.locator('#master-'+key).selectOption(edited?'false':'true')
    else await page.locator('#master-'+key).fill(String(value))
  }
  if(operation.resource==='customers'||operation.resource==='suppliers'){
    await page.locator('#master-currency_id').selectOption(currencyId)
  }
}
async function verifyForm(page:Page,operation:typeof operations[number],row:Row){
  for(const key of ['code','name',...Object.keys(operation.fields),...(row.currency_id?['currency_id']:[])]){
    const value=typeof row[key]==='boolean'?String(row[key]):String(row[key]??'')
    await expect(page.locator('#master-'+key)).toHaveValue(value)
  }
  await expect(page.getByRole('checkbox',{name:'啟用此'+operation.noun,exact:true})).not.toBeChecked()
}

test('四個主檔實際CRUD、搜尋、取消不送出變更、錯誤保護與F5',async({page,request})=>{
  test.setTimeout(240_000)
  const mutations:{method:string;url:string}[]=[]
  page.on('request',r=>{if(['POST','PUT','PATCH','DELETE'].includes(r.method()))mutations.push({method:r.method(),url:r.url()})})
  const cancelEvidence:Record<string,unknown>={}
  for(const operation of operations){
    await page.goto('/')
    await page.getByRole('button',{name:operation.title,exact:true}).click()
    await expect(page.getByRole('heading',{name:operation.title,exact:true})).toBeVisible()
    if(operation.resource==='customers'){
      await page.getByRole('button',{name:'＋ 新增幣別',exact:true}).click()
      const dialog=page.getByRole('dialog')
      await dialog.getByLabel('幣別代碼',{exact:true}).fill('R5-CUR')
      await dialog.getByLabel('幣別名稱',{exact:true}).fill('測試幣別')
      await dialog.getByRole('button',{name:'新增並選取',exact:true}).click()
      await expect(dialog).not.toBeVisible()
      const currencies=await (await request.get('/api/currencies')).json()
      currencyId=currencies[0].id
      expect(currencies[0].rounding_scale).toBeNull();expect(currencies[0].rounding_mode).toBeNull()
    }
    await fill(page,operation,'R5-A',operation.noun+'一號')
    await page.getByRole('button',{name:'儲存'+operation.noun,exact:true}).click()
    await expect(page.getByRole('status')).toHaveText(operation.noun+'已新增。')
    const original=(await (await request.get('/api/'+operation.resource)).json())[0]
    await page.getByRole('button',{name:'新增'+operation.noun,exact:true}).click()
    await fill(page,operation,'R5-B',operation.noun+'二號')
    await page.getByRole('button',{name:'儲存'+operation.noun,exact:true}).click()
    await expect(page.getByRole('button',{name:'開啟 R5-A',exact:true})).toBeVisible()
    await expect(page.getByRole('button',{name:'開啟 R5-B',exact:true})).toBeVisible()
    await page.getByRole('button',{name:'開啟 R5-A',exact:true}).click()
    await page.locator('#master-name').fill('不可保存的取消內容')
    await page.getByRole('checkbox',{name:'啟用此'+operation.noun,exact:true}).uncheck()
    const beforeCancel=mutations.length
    await page.locator('.editor-panel').getByRole('button',{name:'取消',exact:true}).click()
    await expect(page.getByRole('heading',{name:'新增'+operation.noun,exact:true})).toBeVisible()
    await expect(page.locator('#master-name')).toHaveValue('')
    await page.waitForLoadState('networkidle')
    expect(mutations.length).toBe(beforeCancel)
    expect(await (await request.get('/api/'+operation.resource+'/'+original.id)).json()).toEqual(original)
    cancelEvidence[operation.resource]={mutationRequestsBefore:beforeCancel,mutationRequestsAfter:mutations.length,storedRecordUnchanged:true,exitedEditMode:true}
    await page.getByRole('button',{name:'開啟 R5-A',exact:true}).click()
    await fill(page,operation,'R5-A-EDIT',operation.noun+'修訂',true)
    await page.getByRole('checkbox',{name:'啟用此'+operation.noun,exact:true}).uncheck()
    await page.getByRole('button',{name:'儲存變更',exact:true}).click()
    await expect(page.getByRole('status')).toHaveText('變更已儲存。')
    const updated=await (await request.get('/api/'+operation.resource+'/'+original.id)).json()
    expect(updated.id).toBe(original.id);expect(updated.created_at).toBe(original.created_at)
    expect(updated.created_by).toBe(original.created_by);expect(updated.row_version).toBe(2);expect(updated.is_active).toBe(false)
    if(operation.resource==='warehouses')expect(updated.mrp_included).toBe(false)
    const searchField='contact_name' in updated?'contact_name':'address' in updated?'address':'description'
    await page.getByLabel('搜尋'+operation.noun,{exact:true}).fill(updated[searchField])
    await page.getByRole('button',{name:'搜尋',exact:true}).click()
    await expect(page.getByRole('button',{name:'開啟 R5-A-EDIT',exact:true})).toBeVisible()
    await expect(page.getByRole('button',{name:'開啟 R5-B',exact:true})).not.toBeVisible()
    await page.getByLabel('搜尋'+operation.noun,{exact:true}).fill('')
    await page.getByRole('button',{name:'搜尋',exact:true}).click()
    const beforeRejected=await (await request.get('/api/'+operation.resource)).json()
    await page.getByRole('button',{name:'新增'+operation.noun,exact:true}).click()
    await fill(page,operation,'R5-A-EDIT','不得保存的重複')
    await page.getByRole('button',{name:'儲存'+operation.noun,exact:true}).click()
    await expect(page.getByRole('alert')).toContainText('代碼已存在')
    await expectActionFeedback(page)
    if(updated.currency_id){
      expect((await request.put('/api/'+operation.resource+'/'+updated.id,{data:{...body(updated),currency_id:randomUUID()}})).status()).toBe(422)
    }
    expect((await request.post('/api/'+operation.resource,{data:{code:'',name:'invalid',is_active:true}})).status()).toBe(422)
    expect(await (await request.get('/api/'+operation.resource)).json()).toEqual(beforeRejected)
    await page.getByRole('button',{name:'開啟 R5-A-EDIT',exact:true}).click()
    const newerResponse=await request.put('/api/'+operation.resource+'/'+updated.id,{data:{...body(updated),name:operation.noun+'另一端更新'}})
    expect(newerResponse.status()).toBe(200)
    const newer=await newerResponse.json()
    await page.locator('#master-name').fill('過期表單不可覆寫')
    await page.getByRole('button',{name:'儲存變更',exact:true}).click()
    await expect(page.getByRole('alert')).toContainText('另一個視窗更新')
    await expectActionFeedback(page)
    expect(await (await request.get('/api/'+operation.resource+'/'+newer.id)).json()).toEqual(newer)
    await page.getByRole('button',{name:'重新讀取',exact:true}).click()
    await expect(page.locator('#master-name')).toHaveValue(newer.name)
    await page.getByRole('button',{name:'開啟 R5-B',exact:true}).click()
    const deleted=(await (await request.get('/api/'+operation.resource+'?q=R5-B')).json())[0]
    page.once('dialog',d=>d.accept())
    await page.getByRole('button',{name:'刪除'+operation.noun,exact:true}).click()
    await expect(page.getByRole('status')).toHaveText(operation.noun+'已刪除。')
    expect((await request.get('/api/'+operation.resource+'/'+deleted.id)).status()).toBe(404)
    await page.reload()
    await expect(page.getByRole('heading',{name:operation.title,exact:true})).toBeVisible()
    await page.getByRole('button',{name:'開啟 R5-A-EDIT',exact:true}).click()
    await verifyForm(page,operation,newer)
    expect(await (await request.get('/api/'+operation.resource)).json()).toEqual([newer])
    persisted[operation.resource]=newer
  }
  expect(new Set(Object.values(persisted).map(row=>row.id)).size).toBe(4)
  evidence.persistedRecords=persisted;evidence.cancelDesktop=cancelEvidence
  evidence.allFourDesktopCRUD=true;evidence.realDocumentReload=true
  evidence.refreshMethod='Actual page.reload() document refresh, F5-equivalent; not a claimed physical keypress'
})

test('四主檔前後端完整停止與重啟後保留全部欄位與UUID',async({page,request})=>{
  const before={backend:backend.pid,frontend:frontend.pid}
  await stop(frontend);await stop(backend)
  for(const url of [backendUrl+'/api/health',frontendUrl]){
    await expect.poll(async()=>{try{await fetch(url,{signal:AbortSignal.timeout(1500)});return true}catch{return false}}).toBe(false)
  }
  startBackend();startFrontend();await waitReady(backendUrl+'/api/health');await waitReady(frontendUrl)
  expect(backend.pid).not.toBe(before.backend);expect(frontend.pid).not.toBe(before.frontend)
  for(const operation of operations){
    await page.goto('/#'+operation.resource)
    await page.getByRole('button',{name:'開啟 R5-A-EDIT',exact:true}).click()
    const saved=persisted[operation.resource]
    await verifyForm(page,operation,saved)
    expect(await (await request.get('/api/'+operation.resource+'/'+saved.id)).json()).toEqual(saved)
    await page.locator('details.audit summary').click()
    await expect(page.getByTestId('master-uuid')).toHaveText(saved.id)
    await page.evaluate(()=>window.scrollTo(0,0))
    await page.screenshot({path:path.join(artifacts,'master-'+operation.resource+'-desktop.png'),fullPage:true})
  }
  const listeners=JSON.parse(execFileSync('powershell.exe',['-NoProfile','-Command',
    'Get-NetTCPConnection -State Listen -LocalPort 8013,5176 | Select-Object LocalAddress,LocalPort,OwningProcess | ConvertTo-Json'],{windowsHide:true,encoding:'utf8'}))
  expect(listeners).toHaveLength(2)
  expect(listeners.every((l:{LocalAddress:string})=>l.LocalAddress==='127.0.0.1')).toBe(true)
  expect((await request.get(frontendUrl,{headers:{Host:'attacker.invalid'}})).status()).toBe(403)
  const origin=await request.get(frontendUrl,{headers:{Origin:'https://untrusted.invalid'}})
  expect(origin.headers()['access-control-allow-origin']).toBeUndefined()
  const health=await (await request.get('/api/health')).json()
  expect(health.foreign_keys).toBe(1);expect(health.journal_mode).toBe('wal')
  evidence.loopbackListeners=listeners;evidence.health=health
  evidence.fullRestartPersistence=true;evidence.beforePids=before;evidence.afterPids={backend:backend.pid,frontend:frontend.pid}
})

for(const operation of operations)test(operation.title+'手機版與取消不寫入',async({page,request})=>{
  await page.setViewportSize({width:390,height:844})
  await page.goto('/')
  await page.getByRole('button',{name:operation.title,exact:true}).click()
  await expect(page.getByRole('heading',{name:operation.title,exact:true})).toBeVisible()
  await page.getByRole('button',{name:'開啟 R5-A-EDIT',exact:true}).click()
  const before=persisted[operation.resource]
  const mutations:string[]=[]
  page.on('request',r=>{if(['POST','PUT','PATCH','DELETE'].includes(r.method()))mutations.push(r.method())})
  await page.locator('#master-name').fill('手機取消不得保存')
  await page.locator('.editor-panel').getByRole('button',{name:'取消',exact:true}).click()
  await expect(page.getByRole('heading',{name:'新增'+operation.noun,exact:true})).toBeVisible()
  await page.waitForLoadState('networkidle')
  expect(mutations).toEqual([])
  expect(await (await request.get('/api/'+operation.resource+'/'+before.id)).json()).toEqual(before)
  await page.getByRole('button',{name:'開啟 R5-A-EDIT',exact:true}).click()
  await verifyForm(page,operation,before)
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth)).toBe(true)
  await page.screenshot({path:path.join(artifacts,'master-'+operation.resource+'-mobile.png'),fullPage:true})
  evidence['mobile-'+operation.resource]={verified:true,cancelMutationRequests:mutations.length,storedRecordUnchanged:true}
})
