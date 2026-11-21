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
const dbPath=path.join(artifacts,'browser-'+randomUUID()+'.sqlite')
const backendUrl='http://127.0.0.1:8011'
const frontendUrl='http://127.0.0.1:5174'
let backend: ChildProcess, frontend: ChildProcess
const ownedProcesses: ChildProcess[]=[]
const evidence: Record<string,unknown>={database:dbPath,urls:{backend:backendUrl,frontend:frontendUrl},isolated:true}

function startBackend(){
  const py=path.join(root,'.venv',process.platform==='win32'?'Scripts/python.exe':'bin/python')
  backend=spawn(py,['-m','uvicorn','backend.app.main:app','--host','127.0.0.1','--port','8011'],{
    cwd:root,env:{...process.env,ERP_DB_PATH:dbPath},windowsHide:true,stdio:['ignore','pipe','pipe']})
  ownedProcesses.push(backend)
  backend.stdout?.pipe(createWriteStream(path.join(artifacts,'browser-backend.log'),{flags:'a'}))
  backend.stderr?.pipe(createWriteStream(path.join(artifacts,'browser-backend.log'),{flags:'a'}))
}
function startFrontend(){
  frontend=spawn(process.execPath,[path.join(root,'frontend','node_modules','vite','bin','vite.js'),'--host','127.0.0.1','--port','5174','--strictPort'],{
    cwd:path.join(root,'frontend'),env:{...process.env,ERP_API_TARGET:backendUrl},windowsHide:true,stdio:['ignore','pipe','pipe']})
  ownedProcesses.push(frontend)
  frontend.stdout?.pipe(createWriteStream(path.join(artifacts,'browser-frontend.log'),{flags:'a'}))
  frontend.stderr?.pipe(createWriteStream(path.join(artifacts,'browser-frontend.log'),{flags:'a'}))
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
  writeFileSync(path.join(artifacts,'browser-evidence.json'),JSON.stringify(evidence,null,2)+'\n')
})
test('真實原料 CRUD、參照保護、F5 與前後端完整重啟',async({page,request})=>{
  const errors:string[]=[];page.on('pageerror',e=>errors.push(e.message))
  await page.goto('/')
  await expect(page.getByRole('heading',{name:'原料維護',exact:true})).toBeVisible()
  await expect(page.getByText('LOCAL DEMO MODE — NO VERIFIED LOGIN',{exact:true})).toBeVisible()
  await helper(page,'分類','PAPER','紙材')
  await helper(page,'單位','SHEET','張')
  await page.locator('#code').fill('RM-A')
  await page.locator('#name').fill('道林紙')
  await page.locator('#spec').fill('A4 / 80g')
  await page.locator('#lot').selectOption('true')
  await page.getByRole('button',{name:'儲存原料',exact:true}).click()
  await expect(page.getByRole('status')).toHaveText('原料已新增。')
  const original=(await (await request.get('/api/raw-materials?q=RM-A')).json())[0]
  expect(original.item_kind).toBe('RAW')
  await helper(page,'分類','COVER','封面')
  await helper(page,'單位','PIECE','片')
  await page.locator('#name').fill('道林紙（修訂）')
  await page.locator('#spec').fill('B5 / 更新規格')
  await page.locator('#lot').selectOption('false')
  await page.getByRole('checkbox',{name:'啟用此原料'}).uncheck()
  await page.getByRole('button',{name:'儲存變更',exact:true}).click()
  await expect(page.getByRole('status')).toHaveText('變更已儲存。')
  const updated=(await (await request.get('/api/raw-materials/'+original.id)).json())
  expect(updated.id).toBe(original.id);expect(updated.created_at).toBe(original.created_at)
  expect(updated.row_version).toBe(2);expect(updated.is_active).toBe(false);expect(updated.lot_tracking).toBe(false)
  expect(updated.category_id).not.toBe(original.category_id);expect(updated.base_unit_id).not.toBe(original.base_unit_id)
  expect(updated.specification).toBe('B5 / 更新規格')
  await page.getByRole('button',{name:'新增原料',exact:true}).click()
  await form(page,'RM-B','裝訂用膠','PAPER · 紙材','SHEET · 張')
  await page.locator('#spec').fill('可刪除的第二筆測試原料')
  await page.getByRole('button',{name:'儲存原料',exact:true}).click()
  await expect(page.getByRole('button',{name:'開啟 RM-B',exact:true})).toBeVisible()
  await expect(page.getByRole('button',{name:'開啟 RM-A',exact:true})).toBeVisible()
  await page.screenshot({path:path.join(artifacts,'raw-materials-two-records.png'),fullPage:true})

  // Reference failures are genuine UI requests against real SQLite foreign keys.
  for(const [kind,code]of [['分類','PAPER'],['單位','SHEET']] as const){
    await page.getByRole('button',{name:'＋ 新增'+kind,exact:true}).click()
    page.once('dialog',d=>d.accept())
    await page.getByRole('dialog').getByRole('button',{name:'刪除 '+code,exact:true}).click()
    await expect(page.getByRole('dialog').getByRole('alert')).toContainText('引用')
    await expectActionFeedback(page,'.helper-row')
    await page.getByRole('button',{name:'關閉小工具'}).click()
  }
  await page.getByLabel('搜尋品號或品名',{exact:true}).fill('RM-A')
  await page.getByRole('button',{name:'搜尋',exact:true}).click()
  await expect(page.getByRole('button',{name:'開啟 RM-B',exact:true})).not.toBeVisible()
  await page.getByLabel('搜尋品號或品名',{exact:true}).fill('裝訂')
  await page.getByRole('button',{name:'搜尋',exact:true}).click()
  await expect(page.getByRole('button',{name:'開啟 RM-B',exact:true})).toBeVisible()
  await page.getByLabel('搜尋品號或品名',{exact:true}).fill('')
  await page.getByRole('button',{name:'搜尋',exact:true}).click()
  await page.getByRole('button',{name:'新增原料',exact:true}).click()
  await form(page,'RM-A','重複品號','COVER · 封面','PIECE · 片')
  await page.getByRole('button',{name:'儲存原料',exact:true}).click()
  await expect(page.getByRole('alert')).toContainText('品號已存在')
    await expectActionFeedback(page)
  const invalid=await request.post('/api/raw-materials',{data:{code:'INVALID',name:'無效參照',
    specification:null,category_id:randomUUID(),base_unit_id:updated.base_unit_id,lot_tracking:false,is_active:true}})
  expect(invalid.status()).toBe(422)
  expect((await invalid.json()).message).toContain('分類不存在')
  expect((await (await request.get('/api/raw-materials')).json()).length).toBe(2)
  await page.getByRole('button',{name:'開啟 RM-B',exact:true}).click()
  const deleted=(await (await request.get('/api/raw-materials?q=RM-B')).json())[0]
  page.once('dialog',d=>d.accept())
  await page.getByRole('button',{name:'刪除原料',exact:true}).click()
  await expect(page.getByRole('status')).toHaveText('原料已刪除。')
  expect((await request.get('/api/raw-materials/'+deleted.id)).status()).toBe(404)
  // Chromium headless does not dispatch browser-chrome F5 accelerators.
  // reload() performs the genuine document refresh, rather than counting an inert key event.
  await page.reload()
  await expect(page.getByRole('button',{name:'開啟 RM-A',exact:true})).toBeVisible()
  expect((await (await request.get('/api/raw-materials/'+updated.id)).json())).toEqual(updated)
  evidence.f5EquivalentRefreshPersistence=true
  evidence.refreshMethod='Real document reload (F5-equivalent); not a claimed physical key press'

  const oldBackendPid=backend.pid,oldFrontendPid=frontend.pid
  await stop(frontend);await stop(backend)
  for(const url of [backendUrl+'/api/health',frontendUrl]){
    await expect.poll(async()=>{try{await fetch(url,{signal:AbortSignal.timeout(1500)});return true}catch{return false}}).toBe(false)
  }
  startBackend();startFrontend();await waitReady(backendUrl+'/api/health');await waitReady(frontendUrl)
  expect(backend.pid).not.toBe(oldBackendPid);expect(frontend.pid).not.toBe(oldFrontendPid)
  await page.goto('/')
  await expect(page.getByRole('button',{name:'開啟 RM-A',exact:true})).toBeVisible()
  expect((await (await request.get('/api/raw-materials/'+updated.id)).json())).toEqual(updated)
  await page.getByRole('button',{name:'開啟 RM-A',exact:true}).click()
  await expect(page.locator('#name')).toHaveValue(updated.name)
  await expect(page.locator('#spec')).toHaveValue(updated.specification)
  await expect(page.locator('#category')).toHaveValue(updated.category_id)
  await expect(page.locator('#unit')).toHaveValue(updated.base_unit_id)
  await expect(page.locator('#lot')).toHaveValue('false')
  await expect(page.getByRole('checkbox',{name:'啟用此原料'})).not.toBeChecked()
  await page.screenshot({path:path.join(artifacts,'raw-materials-after-restart.png'),fullPage:true})
  evidence.fullRestartPersistence=true;evidence.beforePids={backend:oldBackendPid,frontend:oldFrontendPid}
  evidence.afterPids={backend:backend.pid,frontend:frontend.pid};evidence.persistedRecord=updated
  for(const name of ['採購管理','製造生產','銷售管理','庫存管理','查詢與報表']){
    await page.getByRole('button',{name,exact:false}).first().click()
    if(name==='採購管理')await expect(page.getByRole('heading',{name:'採購訂單管理',exact:true})).toBeVisible()
    else await expect(page.getByText('以下功能尚未實作。',{exact:true})).toBeVisible()
  }
  await page.getByRole('button',{name:'返回原料維護',exact:true}).click()
  expect(errors).toEqual([])
  evidence.browserPageErrors=errors;evidence.crudAndReferenceProtection=true
})
test('窄螢幕仍可操作原料維護',async({page})=>{
  await page.setViewportSize({width:390,height:844})
  await page.goto('/')
  await expect(page.getByRole('heading',{name:'原料維護',exact:true})).toBeVisible()
  await page.getByRole('button',{name:'開啟 RM-A',exact:true}).click()
  await expect(page.locator('#name')).toHaveValue('道林紙（修訂）')
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth)).toBe(true)
  await page.screenshot({path:path.join(artifacts,'raw-materials-mobile.png'),fullPage:true})
})

