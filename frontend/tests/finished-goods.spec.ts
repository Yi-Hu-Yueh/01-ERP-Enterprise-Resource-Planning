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
const dbPath=path.join(artifacts,'browser-finished-'+randomUUID()+'.sqlite')
const backendUrl='http://127.0.0.1:8012'
const frontendUrl='http://127.0.0.1:5175'
let backend: ChildProcess, frontend: ChildProcess
const ownedProcesses: ChildProcess[]=[]
const evidence: Record<string,unknown>={database:dbPath,urls:{backend:backendUrl,frontend:frontendUrl},isolated:true}

function startBackend(){
  const py=path.join(root,'.venv',process.platform==='win32'?'Scripts/python.exe':'bin/python')
  backend=spawn(py,['-m','uvicorn','backend.app.main:app','--host','127.0.0.1','--port','8012'],{
    cwd:root,env:{...process.env,ERP_DB_PATH:dbPath},windowsHide:true,stdio:['ignore','pipe','pipe']})
  ownedProcesses.push(backend)
  backend.stdout?.pipe(createWriteStream(path.join(artifacts,'finished-backend.log'),{flags:'a'}))
  backend.stderr?.pipe(createWriteStream(path.join(artifacts,'finished-backend.log'),{flags:'a'}))
}
function startFrontend(){
  frontend=spawn(process.execPath,[path.join(root,'frontend','node_modules','vite','bin','vite.js'),'--host','127.0.0.1','--port','5175','--strictPort'],{
    cwd:path.join(root,'frontend'),env:{...process.env,ERP_API_TARGET:backendUrl},windowsHide:true,stdio:['ignore','pipe','pipe']})
  ownedProcesses.push(frontend)
  frontend.stdout?.pipe(createWriteStream(path.join(artifacts,'finished-frontend.log'),{flags:'a'}))
  frontend.stderr?.pipe(createWriteStream(path.join(artifacts,'finished-frontend.log'),{flags:'a'}))
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
async function helper(page:Page,kind:'分類'|'單位',code:string,name:string){
  await page.getByRole('button',{name:'＋ 新增'+kind,exact:true}).click()
  const dialog=page.getByRole('dialog')
  await dialog.getByLabel('代碼',{exact:true}).fill(code)
  await dialog.getByLabel('名稱',{exact:true}).fill(name)
  await dialog.getByRole('button',{name:'新增並選取',exact:true}).click()
  await expect(dialog).not.toBeVisible()
}
async function form(page:Page,code:string,name:string,cat:string,unit:string){
  await page.locator('#code').fill(code)
  await page.locator('#name').fill(name)
  await page.getByLabel('分類',{exact:false}).selectOption({label:cat})
  await page.getByLabel('基本單位',{exact:false}).selectOption({label:unit})
  await page.getByLabel('批次管理',{exact:false}).selectOption('true')
}
test.beforeAll(async()=>{
  // Refuse to touch unrelated services if the dedicated QA ports are already occupied.
  for(const url of [backendUrl+'/api/health',frontendUrl]){
    let live=false;try{live=(await fetch(url,{signal:AbortSignal.timeout(1500)})).ok}catch{}
    if(live)throw new Error('QA port already in use; no process was stopped: '+url)
  }
  startBackend();startFrontend();await waitReady(backendUrl+'/api/health');await waitReady(frontendUrl)
})
test.afterAll(async()=>{
  for(const child of [...ownedProcesses].reverse())if(child.exitCode===null)await stop(child)
  writeFileSync(path.join(artifacts,'finished-goods-browser-evidence.json'),JSON.stringify(evidence,null,2)+'\n')
})

test.use({baseURL:frontendUrl})

test('成品共用主檔、雙向隔離、失敗保護、重新整理與完整重啟',async({page,request})=>{
  await page.goto('/')
  await helper(page,'分類','PAPER','紙材')
  await helper(page,'單位','SHEET','張')
  await page.locator('#code').fill('RM-KEEP')
  await page.locator('#name').fill('保留原料')
  await page.locator('#spec').fill('原料原有規格')
  await page.locator('#lot').selectOption('true')
  await page.getByRole('button',{name:'儲存原料',exact:true}).click()
  await expect(page.getByRole('status')).toHaveText('原料已新增。')
  const raw=(await (await request.get('/api/raw-materials')).json())[0]

  await page.getByRole('button',{name:'成品資料維護',exact:true}).click()
  await expect(page.getByRole('heading',{name:'成品資料維護',exact:true})).toBeVisible()
  await expect(page.getByRole('button',{name:'開啟 RM-KEEP',exact:true})).not.toBeVisible()
  await helper(page,'分類','BOOK','簿本')
  await helper(page,'單位','BOOK','本')
  await page.locator('#code').fill('FG-A')
  await page.locator('#name').fill('精裝筆記本')
  await page.locator('#spec').fill('A5')
  await page.locator('#lot').selectOption('true')
  await page.getByRole('button',{name:'儲存成品',exact:true}).click()
  await expect(page.getByRole('status')).toHaveText('成品已新增。')
  const first=(await (await request.get('/api/finished-goods?q=FG-A')).json())[0]
  expect(first.item_kind).toBe('FINISHED')
  expect(first.id).not.toBe(raw.id)
  await page.getByRole('button',{name:'新增成品',exact:true}).click()
  await form(page,'FG-B','可刪除成品','BOOK · 簿本','BOOK · 本')
  await page.getByRole('button',{name:'儲存成品',exact:true}).click()
  await expect(page.getByRole('button',{name:'開啟 FG-B',exact:true})).toBeVisible()
  await expect(page.getByRole('button',{name:'開啟 FG-A',exact:true})).toBeVisible()
  await page.screenshot({path:path.join(artifacts,'finished-goods-two-records.png'),fullPage:true})
  for(const [query,visible,hidden] of [['FG-A','FG-A','FG-B'],['可刪除','FG-B','FG-A']]){
    await page.getByLabel('搜尋品號或品名',{exact:true}).fill(query)
    await page.getByRole('button',{name:'搜尋',exact:true}).click()
    await expect(page.getByRole('button',{name:'開啟 '+visible,exact:true})).toBeVisible()
    await expect(page.getByRole('button',{name:'開啟 '+hidden,exact:true})).not.toBeVisible()
  }
  await page.getByLabel('搜尋品號或品名',{exact:true}).fill('RM-KEEP')
  await page.getByRole('button',{name:'搜尋',exact:true}).click()
  await expect(page.getByRole('heading',{name:'沒有符合的成品',exact:true})).toBeVisible()
  await page.getByLabel('搜尋品號或品名',{exact:true}).fill('')
  await page.getByRole('button',{name:'搜尋',exact:true}).click()
  await page.getByRole('button',{name:'開啟 FG-A',exact:true}).click()
  await form(page,'FG-A-REVISED','修訂成品','PAPER · 紙材','SHEET · 張')
  await page.locator('#spec').fill('B5 / 新規格')
  await page.locator('#lot').selectOption('false')
  await page.getByRole('checkbox',{name:'啟用此成品',exact:true}).uncheck()
  await page.getByRole('button',{name:'儲存變更',exact:true}).click()
  await expect(page.getByRole('status')).toHaveText('變更已儲存。')
  const updated=await (await request.get('/api/finished-goods/'+first.id)).json()
  expect(updated.id).toBe(first.id)
  expect(updated.created_at).toBe(first.created_at)
  expect(updated.created_by).toBe(first.created_by)
  expect(updated.row_version).toBe(2)
  expect(updated.item_kind).toBe('FINISHED')
  expect(updated.lot_tracking).toBe(false)
  expect(updated.is_active).toBe(false)
  const body={code:updated.code,name:updated.name,specification:updated.specification,
    category_id:updated.category_id,base_unit_id:updated.base_unit_id,lot_tracking:false,is_active:false}
  const snapshot=async()=>({raw:await (await request.get('/api/raw-materials')).json(),finished:await (await request.get('/api/finished-goods')).json()})
  const beforeRejected=await snapshot()
  for(const code of ['FG-A-REVISED','RM-KEEP']){
    await page.getByRole('button',{name:'新增成品',exact:true}).click()
    await form(page,code,'不得留下的重複成品','PAPER · 紙材','SHEET · 張')
    await page.getByRole('button',{name:'儲存成品',exact:true}).click()
    await expect(page.getByRole('alert')).toContainText('品號已存在')
    await expectActionFeedback(page)
  }
  const rejected:Record<string,number>={}
  for(const field of ['category_id','base_unit_id']){
    const invalid=await request.post('/api/finished-goods',{data:{...body,code:'BAD-'+field,[field]:randomUUID()}})
    expect(invalid.status()).toBe(422);rejected[field]=invalid.status()
  }
  for(const [endpoint,foreign]of [['raw-materials',updated],['finished-goods',raw]] as const){
    const url='/api/'+endpoint+'/'+foreign.id
    const read=await request.get(url),edit=await request.put(url,{data:{...body,row_version:foreign.row_version}})
    const del=await request.delete(url+'?row_version='+foreign.row_version)
    expect([read.status(),edit.status(),del.status()]).toEqual([404,404,404])
    rejected[endpoint+'-cross-read']=read.status();rejected[endpoint+'-cross-edit']=edit.status();rejected[endpoint+'-cross-delete']=del.status()
  }
  const tampered=await request.put('/api/finished-goods/'+updated.id,{data:{...body,row_version:2,item_kind:'RAW'}})
  expect(tampered.status()).toBe(422)
  expect(await snapshot()).toEqual(beforeRejected)
  evidence.rejectedRequests=rejected;evidence.failedOperationsAtomic=true

  // A second API client commits; the still-open browser form holds version 2.
  await page.getByRole('button',{name:'開啟 FG-A-REVISED',exact:true}).click()
  const newer=await request.put('/api/finished-goods/'+updated.id,{data:{...body,name:'另一客戶端更新',row_version:2}})
  expect(newer.status()).toBe(200)
  const persisted=await newer.json()
  await page.locator('#name').fill('過期表單不可覆寫')
  await page.getByRole('button',{name:'儲存變更',exact:true}).click()
  await expect(page.getByRole('alert')).toContainText('另一個視窗更新')
    await expectActionFeedback(page)
  expect(await (await request.get('/api/finished-goods/'+updated.id)).json()).toEqual(persisted)
  await page.getByRole('button',{name:'重新讀取',exact:true}).click()
  await expect(page.locator('#name')).toHaveValue(persisted.name)
  for(const [kind,code]of [['分類','PAPER'],['單位','SHEET']] as const){
    await page.getByRole('button',{name:'＋ 新增'+kind,exact:true}).click()
    page.once('dialog',dialog=>dialog.accept())
    await page.getByRole('dialog').getByRole('button',{name:'刪除 '+code,exact:true}).click()
    await expect(page.getByRole('dialog').getByRole('alert')).toContainText('引用')
    await expectActionFeedback(page,'.helper-row')
    await page.getByRole('button',{name:'關閉小工具',exact:true}).click()
  }
  await page.getByRole('button',{name:'開啟 FG-B',exact:true}).click()
  const removed=(await (await request.get('/api/finished-goods?q=FG-B')).json())[0]
  page.once('dialog',dialog=>dialog.accept())
  await page.getByRole('button',{name:'刪除成品',exact:true}).click()
  await expect(page.getByRole('status')).toHaveText('成品已刪除。')
  expect((await request.get('/api/finished-goods/'+removed.id)).status()).toBe(404)
  await page.reload()
  await expect(page.getByRole('heading',{name:'成品資料維護',exact:true})).toBeVisible()
  await expect(page.getByRole('button',{name:'開啟 FG-A-REVISED',exact:true})).toBeVisible()
  expect(await (await request.get('/api/finished-goods/'+first.id)).json()).toEqual(persisted)
  evidence.f5EquivalentRefreshPersistence=true
  evidence.refreshMethod='Actual page.reload() document refresh; not a claimed physical F5 keypress'

  const beforePids={backend:backend.pid,frontend:frontend.pid}
  await stop(frontend);await stop(backend)
  for(const url of [backendUrl+'/api/health',frontendUrl]){
    await expect.poll(async()=>{try{await fetch(url,{signal:AbortSignal.timeout(1500)});return true}catch{return false}}).toBe(false)
  }
  startBackend();startFrontend();await waitReady(backendUrl+'/api/health');await waitReady(frontendUrl)
  expect(backend.pid).not.toBe(beforePids.backend);expect(frontend.pid).not.toBe(beforePids.frontend)
  await page.goto('/#finished-goods')
  await expect(page.getByRole('button',{name:'開啟 FG-A-REVISED',exact:true})).toBeVisible()
  await page.getByRole('button',{name:'開啟 FG-A-REVISED',exact:true}).click()
  for(const [id,value]of [['code',persisted.code],['name',persisted.name],['spec',persisted.specification],
    ['category',persisted.category_id],['unit',persisted.base_unit_id],['lot','false']]){
    await expect(page.locator('#'+id)).toHaveValue(value)
  }
  await expect(page.getByRole('checkbox',{name:'啟用此成品',exact:true})).not.toBeChecked()
  await page.locator('details.audit summary').click()
  await expect(page.getByTestId('record-uuid')).toHaveText(persisted.id)
  expect(await (await request.get('/api/finished-goods/'+first.id)).json()).toEqual(persisted)
  await page.screenshot({path:path.join(artifacts,'finished-goods-after-restart.png'),fullPage:true})
  await page.getByRole('button',{name:'原物料資料維護',exact:true}).click()
  await expect(page.getByRole('button',{name:'開啟 RM-KEEP',exact:true})).toBeVisible()
  await expect(page.getByRole('button',{name:'開啟 FG-A-REVISED',exact:true})).not.toBeVisible()
  await page.getByRole('button',{name:'開啟 RM-KEEP',exact:true}).click()
  await expect(page.locator('#name')).toHaveValue(raw.name)
  expect(await (await request.get('/api/raw-materials/'+raw.id)).json()).toEqual(raw)
  await page.screenshot({path:path.join(artifacts,'raw-materials-retained.png'),fullPage:true})
  await page.reload()
  await expect(page.getByRole('button',{name:'開啟 RM-KEEP',exact:true})).toBeVisible()
  expect(await (await request.get('/api/raw-materials/'+raw.id)).json()).toEqual(raw)
  expect((await snapshot()).raw).toEqual([raw])
  expect((await snapshot()).finished).toEqual([persisted])
  const host=await request.get(frontendUrl,{headers:{Host:'attacker.invalid'}})
  expect(host.status()).toBe(403)
  const origin=await request.get(frontendUrl,{headers:{Origin:'https://untrusted.invalid'}})
  expect(origin.status()).toBe(200);expect(origin.headers()['access-control-allow-origin']).toBeUndefined()
  const outside=await request.get(frontendUrl+'/@fs/'+root.replaceAll('\\','/')+'/README.md')
  expect(outside.status()).toBe(403)
  const listeners=JSON.parse(execFileSync('powershell.exe',['-NoProfile','-Command',
    'Get-NetTCPConnection -State Listen -LocalPort 8012,5175 | Select-Object LocalAddress,LocalPort,OwningProcess | ConvertTo-Json'],{windowsHide:true,encoding:'utf8'}))
  expect(listeners).toHaveLength(2)
  expect(listeners.every((listener:{LocalAddress:string})=>listener.LocalAddress==='127.0.0.1')).toBe(true)
  evidence.loopbackListeners=listeners
  evidence.containment={unapprovedHost:host.status(),foreignOriginAllowsRead:false,outsideFrontend:outside.status()}
  evidence.fullRestartPersistence=true;evidence.beforePids=beforePids
  evidence.afterPids={backend:backend.pid,frontend:frontend.pid}
  evidence.persistedFinished=persisted;evidence.persistedRaw=raw
  evidence.sharedIdentityAndIsolation=true;evidence.staleVersionRejected=true
})

test('窄螢幕成品與原物料入口仍隔離且可操作',async({page})=>{
  await page.setViewportSize({width:390,height:844})
  await page.goto('/#finished-goods')
  await expect(page.getByRole('heading',{name:'成品資料維護',exact:true})).toBeVisible()
  await page.getByRole('button',{name:'開啟 FG-A-REVISED',exact:true}).click()
  await expect(page.locator('#name')).toHaveValue('另一客戶端更新')
  await expect(page.locator('#lot')).toHaveValue('false')
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth)).toBe(true)
  await page.screenshot({path:path.join(artifacts,'finished-goods-mobile.png'),fullPage:true})
  await page.getByRole('button',{name:'原物料資料維護',exact:true}).click()
  await expect(page.getByRole('button',{name:'開啟 RM-KEEP',exact:true})).toBeVisible()
  await expect(page.getByRole('button',{name:'開啟 FG-A-REVISED',exact:true})).not.toBeVisible()
  await page.getByRole('button',{name:'成品資料維護',exact:true}).click()
  await expect(page.getByRole('button',{name:'開啟 FG-A-REVISED',exact:true})).toBeVisible()
  await page.getByRole('button',{name:'其他基本資料',exact:false}).click()
  await expect(page.getByText('以下功能尚未實作。',{exact:true})).toBeVisible()
  await page.getByRole('button',{name:'返回原料維護',exact:true}).click()
  await expect(page.getByRole('heading',{name:'原料維護',exact:true})).toBeVisible()
  evidence.mobileBothViewsVerified=true
})
