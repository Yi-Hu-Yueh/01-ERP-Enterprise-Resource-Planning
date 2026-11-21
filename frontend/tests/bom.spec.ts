import {test,expect,type Page} from '@playwright/test'
import {spawn,execFileSync,type ChildProcess} from 'node:child_process'
import {mkdirSync,createWriteStream,writeFileSync,readFileSync} from 'node:fs'
import path from 'node:path'
import {fileURLToPath} from 'node:url'
import {randomUUID} from 'node:crypto'
import {expectActionFeedback} from './feedback-assertions'

const root=path.resolve(fileURLToPath(new URL('../../',import.meta.url)))
const artifacts=path.resolve(root,process.env.ERP_TEST_ARTIFACTS||'artifacts/manual-round11/browser'),task=path.dirname(artifacts)
mkdirSync(artifacts,{recursive:true})
const database=path.join(task,'browser-bom-'+randomUUID()+'.sqlite')
const py=path.join(root,'.venv','Scripts','python.exe'),apiUrl='http://127.0.0.1:8019',uiUrl='http://127.0.0.1:5182'
let launcher:ChildProcess,fixtures:any,before:any,productA:any,productB:any
const children:ChildProcess[]=[],evidence:Record<string,any>={isolated:true,database,urls:{apiUrl,uiUrl},generatedScreenshots:0}
const precise='0.123456789012345678901234567890123456789'
test.use({baseURL:uiUrl,screenshot:'off',trace:'off'})
test.describe.configure({mode:'serial'})
async function ready(url:string){await expect.poll(async()=>{try{return(await fetch(url,{signal:AbortSignal.timeout(1000)})).status}catch{return 0}},{timeout:40_000}).toBe(200)}
async function offline(url:string){await expect.poll(async()=>{try{return(await fetch(url,{signal:AbortSignal.timeout(1000)})).status}catch{return 0}},{timeout:20_000}).toBe(0)}
async function start(){
 launcher=spawn(py,['-u','scripts/dev.py','--backend-port','8019','--frontend-port','5182','--database',database],{cwd:root,windowsHide:true,stdio:['ignore','pipe','pipe']});children.push(launcher)
 launcher.stdout?.pipe(createWriteStream(path.join(artifacts,'launcher.log'),{flags:'a'}));launcher.stderr?.pipe(createWriteStream(path.join(artifacts,'launcher-error.log'),{flags:'a'}))
 await ready(apiUrl+'/api/health');await ready(uiUrl);expect(launcher.exitCode).toBeNull()
}
function listeners(){return JSON.parse(execFileSync('powershell.exe',['-NoProfile','-Command',"ConvertTo-Json -InputObject @(Get-NetTCPConnection -State Listen | Where-Object LocalPort -in 8019,5182 | Select-Object LocalAddress,LocalPort,OwningProcess) -Compress"],{windowsHide:true,encoding:'utf8'}))}
async function stop(child:ChildProcess){
 if(child.exitCode!==null)throw Error('Owned QA launcher exited unexpectedly')
 execFileSync('taskkill.exe',['/PID',String(child.pid),'/T','/F'],{windowsHide:true,stdio:'pipe'})
 await new Promise<void>(resolve=>{if(child.exitCode!==null)resolve();else child.once('exit',()=>resolve())})
 await offline(apiUrl+'/api/health');await offline(uiUrl)
 expect(listeners()).toEqual([])
}
function snapshot(){return JSON.parse(execFileSync(py,['-c',"import json,sys;from backend.tests.test_supplier_offers import snapshot;print(json.dumps(snapshot(sys.argv[1])))",database],{cwd:root,windowsHide:true,encoding:'utf8'}))}
function legacy(s:any){return Object.fromEntries(Object.entries(s.rows).filter(([t])=>!['bom','bom_component','stock_create_receipt'].includes(t)))}
async function json(url='/api/boms'){const r=await fetch(uiUrl+url);expect(r.status).toBe(200);return r.json()}
function watch(page:Page){page.on('pageerror',e=>{throw e})}
async function newHeader(page:Page,product:number,revision:string,basis='10'){
 await page.getByRole('button',{name:'＋ 新增 BOM',exact:true}).click()
 await page.locator('#bom-product').selectOption(fixtures.products[product].id);await page.locator('#bom-revision').fill(revision)
 await page.locator('#bom-basis').fill(basis);await page.locator('#bom-from').fill('2026-11-19');await page.locator('#bom-active').selectOption('true')
 const response=page.waitForResponse(r=>r.url().endsWith('/api/boms')&&r.request().method()==='POST')
 await page.getByRole('button',{name:'儲存表頭',exact:true}).click();const r=await response;expect(r.status()).toBe(201)
 await expect(page.getByRole('button',{name:'編輯表頭',exact:true})).toBeVisible()
 return r.json()
}
async function add(page:Page,material:number,qty:string,purpose:string){
 await page.getByRole('button',{name:'＋ 新增用料',exact:true}).click()
 await page.locator('#bom-material').selectOption(fixtures.raws[material].id);await page.locator('#bom-usage').fill(qty);await page.locator('#bom-purpose').fill(purpose)
 const response=page.waitForResponse(r=>r.url().includes('/components')&&r.request().method()==='POST')
 await page.getByRole('button',{name:'儲存明細',exact:true}).click();expect((await response).status()).toBe(201)
 await expect(page.getByRole('form',{name:'新增用料明細',exact:true})).toHaveCount(0)
}
async function open(page:Page,product:number,revision:string){
 await page.getByRole('button',{name:'開啟 BOM '+fixtures.products[product].code+' '+revision,exact:true}).click()
 await expect(page.getByTestId('bom-revision-value')).toHaveText(revision)
}
test.beforeAll(async()=>{
 for(const url of [apiUrl,uiUrl]){let occupied=false;try{await fetch(url,{signal:AbortSignal.timeout(800)});occupied=true}catch{}if(occupied)throw Error('QA port occupied; no process stopped')}
 const output=execFileSync(py,[path.join(root,'backend/tests/seed_bom_browser.py'),database,task],{cwd:root,windowsHide:true,encoding:'utf8'})
 writeFileSync(path.join(artifacts,'seed-output.txt'),output);fixtures=JSON.parse(readFileSync(path.join(task,'bom-browser-fixtures.json'),'utf8'))
 before=snapshot();await start();evidence.initialListeners=listeners();evidence.beforeBusinessRows=legacy(before)
})
test.afterAll(async()=>{
 for(const child of [...children].reverse())if(child.exitCode===null)await stop(child)
 const after=snapshot();expect(legacy(after)).toEqual(legacy(before))
 evidence.afterBusinessRows=legacy(after);evidence.finalSnapshot=after;evidence.qaServicesStopped=true
 writeFileSync(path.join(artifacts,'bom-browser-evidence.json'),JSON.stringify(evidence,null,2)+'\n')
})

test('desktop genuine A/B BOM CRUD, repeated components, units, exact usage and document F5',async({page})=>{
 watch(page);await page.goto('/#boms');await expect(page.getByRole('heading',{name:'產品 BOM 用料表',exact:true})).toBeVisible()
 productA=await newHeader(page,0,'A','10.000000000000000000000000001')
 await add(page,0,precise,'內頁');await add(page,1,'12.75','封面');await add(page,0,'2','其他用途')
 await expect(page.locator('[data-testid^="bom-component-"]')).toHaveCount(3)
 await expect(page.getByTestId('bom-usage-1')).toContainText(precise+' SHEET')
 await expect(page.getByTestId('bom-basis-value')).toHaveText('10.000000000000000000000000001 SHEET · 張')
 productB=await newHeader(page,1,'B','1');await add(page,0,'9.25','共用原料')
 const savedB=await json('/api/boms/'+productB.id)
 await open(page,0,'A');await page.getByRole('button',{name:'編輯用料 1',exact:true}).click()
 await expect(page.getByRole('form',{name:'編輯用料明細',exact:true})).toHaveCount(1)
 await expect(page.getByRole('button',{name:'編輯用料 2',exact:true})).toBeDisabled();await expect(page.getByRole('button',{name:'＋ 新增用料',exact:true})).toBeDisabled()
 await page.locator('#bom-usage').fill('4.000000000000000000000000001');await page.getByRole('button',{name:'儲存明細',exact:true}).click()
 await expect(page.getByTestId('bom-usage-1')).toContainText('4.000000000000000000000000001')
 expect(await json('/api/boms/'+productB.id)).toEqual(savedB)
 await page.locator('#bom-search').fill('QA-R1');const search=page.waitForResponse(r=>r.url().includes('/api/boms?q='))
 await page.getByRole('button',{name:'搜尋',exact:true}).click();await search
 await expect(page.getByRole('button',{name:/開啟 BOM/})).toHaveCount(2)
 const saved=await json();await page.reload({waitUntil:'networkidle'})
 expect(await page.evaluate(()=>performance.getEntriesByType('navigation').map(e=>(e as PerformanceNavigationTiming).type))).toContain('reload')
 await open(page,0,'A');await expect(page.locator('[data-testid^="bom-component-"]')).toHaveCount(3)
 expect(await json()).toEqual(saved);evidence.actualDocumentReload=true;evidence.desktopManyToManyExactAndSingleEditor=true
})

test('header/detail/new cancellation has zero mutations, protected errors stay near controls, eligible deletes',async({page})=>{
 watch(page);await page.goto('/#boms');await open(page,0,'A');const saved=snapshot();let mutations=0
 const count=(r:any)=>{if(['POST','PUT','DELETE','PATCH'].includes(r.method()))mutations++};page.on('request',count)
 await page.getByRole('button',{name:'編輯表頭',exact:true}).click();await page.locator('#bom-revision').fill('UNSAVED')
 await page.getByRole('button',{name:'取消',exact:true}).click();await expect(page.getByTestId('bom-revision-value')).toHaveText('A')
 await page.getByRole('button',{name:'編輯用料 1',exact:true}).click();await page.locator('#bom-usage').fill('999')
 await page.getByRole('form',{name:'編輯用料明細',exact:true}).getByRole('button',{name:'取消',exact:true}).click()
 await page.getByRole('button',{name:'＋ 新增用料',exact:true}).click();await page.locator('#bom-usage').fill('88')
 await page.getByRole('form',{name:'新增用料明細',exact:true}).getByRole('button',{name:'取消',exact:true}).click()
 await page.getByRole('button',{name:'＋ 新增 BOM',exact:true}).click();await page.locator('#bom-revision').fill('UNSAVED')
 await page.getByRole('button',{name:'取消',exact:true}).click();await expect(page.locator('#bom-revision')).toHaveValue('')
 expect(mutations).toBe(0);expect(snapshot()).toEqual(saved);page.off('request',count)
 await open(page,0,'A');page.once('dialog',d=>d.accept());await page.getByRole('button',{name:'刪除 BOM',exact:true}).click()
 await expect(page.getByRole('alert')).toContainText('仍有用料明細');await expectActionFeedback(page,'section[aria-label="BOM 表頭"] .form-actions')
 await page.getByRole('button',{name:'編輯用料 1',exact:true}).click();await page.locator('#bom-usage').fill('NaN');await page.getByRole('button',{name:'儲存明細',exact:true}).click()
 await expect(page.getByRole('alert')).toContainText('標準用量');await expectActionFeedback(page,'form[aria-label="編輯用料明細"] .form-actions')
 await page.getByRole('form',{name:'編輯用料明細',exact:true}).getByRole('button',{name:'取消',exact:true}).click()
 expect(snapshot()).toEqual(saved)
 const temp=await newHeader(page,0,'DELETE-ME');await add(page,0,'2','可刪除')
 page.once('dialog',d=>d.accept());await page.getByRole('button',{name:'刪除用料 1',exact:true}).click();await expect(page.locator('[data-testid^="bom-component-"]')).toHaveCount(0)
 await page.getByRole('button',{name:'編輯表頭',exact:true}).click();await page.locator('#bom-revision').fill('DELETE-UPDATED')
 await page.getByRole('button',{name:'儲存表頭',exact:true}).click();await expect(page.getByTestId('bom-revision-value')).toHaveText('DELETE-UPDATED')
 page.once('dialog',d=>d.accept());await page.getByRole('button',{name:'刪除 BOM',exact:true}).click();await expect(page.locator('#bom-revision')).toHaveValue('')
 expect((await json()).some((r:any)=>r.id===temp.id)).toBe(false)
 evidence.cancelMutations=mutations;evidence.cancelExactSnapshotPreserved=true;evidence.actionFeedbackAndEligibleDeletes=true
 evidence.beforeRestart={rows:await json(),snapshot:snapshot(),listeners:listeners()}
})

test('both QA services fully stop/restart, mobile BOM cancel/feedback and existing purchase navigation persist',async({page})=>{
 watch(page);await stop(launcher);evidence.offlineListeners=listeners();await start()
 const next=listeners();expect(next).toHaveLength(2)
 expect(next.every((r:any)=>r.LocalAddress==='127.0.0.1'&&!evidence.beforeRestart.listeners.some((p:any)=>p.OwningProcess===r.OwningProcess))).toBe(true)
 expect(await json()).toEqual(evidence.beforeRestart.rows);expect(snapshot()).toEqual(evidence.beforeRestart.snapshot)
 await page.setViewportSize({width:390,height:844});await page.goto('/#boms');await open(page,0,'A')
 await expect(page.locator('[data-testid^="bom-component-"]')).toHaveCount(3)
 expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true)
 const beforeCancel=snapshot();let mutations=0;const count=(r:any)=>{if(['POST','PUT','DELETE','PATCH'].includes(r.method()))mutations++};page.on('request',count)
 await page.getByRole('button',{name:'編輯用料 2',exact:true}).click();await page.locator('#bom-purpose').fill('手機未儲存')
 const form=page.getByRole('form',{name:'編輯用料明細',exact:true})
 await expect(form.getByRole('button',{name:'取消',exact:true})).toBeVisible();await form.getByRole('button',{name:'取消',exact:true}).click()
 await page.getByRole('button',{name:'編輯表頭',exact:true}).click();await page.locator('#bom-revision').fill('手機未儲存')
 await page.getByRole('button',{name:'取消',exact:true}).click()
 expect(mutations).toBe(0);expect(snapshot()).toEqual(beforeCancel);page.off('request',count)
 page.once('dialog',d=>d.accept());await page.getByRole('button',{name:'刪除 BOM',exact:true}).click();await expectActionFeedback(page,'section[aria-label="BOM 表頭"] .form-actions')
 await page.getByRole('button',{name:'採購管理',exact:false}).filter({hasText:'採購管理'}).first().click()
 await expect(page.getByRole('heading',{name:'採購訂單管理',exact:true})).toBeVisible()
 await page.getByRole('button',{name:'未到貨採購查詢',exact:true}).filter({visible:true}).click();await expect(page.getByTestId('oq-order-count')).toHaveText('2')
 expect(await json('/api/purchase-outstanding')).toEqual(fixtures.inquiry);expect(await json('/api/stock-positions')).toEqual(fixtures.stock)
 await page.getByRole('button',{name:'製造生產',exact:false}).first().click();await expect(page.getByRole('heading',{name:'產品 BOM 用料表',exact:true})).toBeVisible()
 await open(page,1,'B');await expect(page.getByTestId('bom-basis-value')).toHaveText('1 BOOK · 本');await expect(page.getByTestId('bom-usage-1')).toContainText('9.25 SHEET · 張')
 expect(legacy(snapshot())).toEqual(legacy(before));evidence.mobileCancellationMutations=mutations;evidence.mobileNoHorizontalOverflow=true
 evidence.afterRestart={listeners:next,rows:await json(),snapshot:snapshot(),mobileAndExistingNavigation:true}
})
