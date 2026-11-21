import {test,expect,type Page} from '@playwright/test'
import {spawn,execFileSync,type ChildProcess} from 'node:child_process'
import {mkdirSync,createWriteStream,writeFileSync,readFileSync} from 'node:fs'
import path from 'node:path'
import {fileURLToPath} from 'node:url'
import {randomUUID} from 'node:crypto'
import {expectActionFeedback} from './feedback-assertions'

const root=path.resolve(fileURLToPath(new URL('../../',import.meta.url)))
const artifacts=path.resolve(root,process.env.ERP_TEST_ARTIFACTS||'artifacts/manual-round12/browser'),task=path.dirname(artifacts)
mkdirSync(artifacts,{recursive:true})
const database=path.join(task,'browser-routing-'+randomUUID()+'.sqlite')
const py=path.join(root,'.venv','Scripts','python.exe'),apiUrl='http://127.0.0.1:8020',uiUrl='http://127.0.0.1:5183'
let launcher:ChildProcess,fixtures:any,before:any,routeA:any,routeB:any
const children:ChildProcess[]=[],evidence:Record<string,any>={isolated:true,database,urls:{apiUrl,uiUrl},generatedScreenshots:0}
const precise='0.123456789012345678901234567890123456789'
test.use({baseURL:uiUrl,screenshot:'off',trace:'off'})
test.describe.configure({mode:'serial'})
async function ready(url:string){await expect.poll(async()=>{try{return(await fetch(url,{signal:AbortSignal.timeout(1000)})).status}catch{return 0}},{timeout:40_000}).toBe(200)}
async function offline(url:string){await expect.poll(async()=>{try{return(await fetch(url,{signal:AbortSignal.timeout(1000)})).status}catch{return 0}},{timeout:20_000}).toBe(0)}
async function start(){
 launcher=spawn(py,['-u','scripts/dev.py','--backend-port','8020','--frontend-port','5183','--database',database],{cwd:root,windowsHide:true,stdio:['ignore','pipe','pipe']});children.push(launcher)
 launcher.stdout?.pipe(createWriteStream(path.join(artifacts,'launcher.log'),{flags:'a'}));launcher.stderr?.pipe(createWriteStream(path.join(artifacts,'launcher-error.log'),{flags:'a'}))
 await ready(apiUrl+'/api/health');await ready(uiUrl);expect(launcher.exitCode).toBeNull()
}
function listeners(){return JSON.parse(execFileSync('powershell.exe',['-NoProfile','-Command',"ConvertTo-Json -InputObject @(Get-NetTCPConnection -State Listen | Where-Object LocalPort -in 8020,5183 | Select-Object LocalAddress,LocalPort,OwningProcess) -Compress"],{windowsHide:true,encoding:'utf8',stdio:['ignore','pipe','pipe']}))}
async function stop(child:ChildProcess){
 if(child.exitCode!==null)throw Error('Owned QA launcher exited unexpectedly')
 execFileSync('taskkill.exe',['/PID',String(child.pid),'/T','/F'],{windowsHide:true,stdio:'pipe'})
 await new Promise<void>(resolve=>{if(child.exitCode!==null)resolve();else child.once('exit',()=>resolve())})
 await offline(apiUrl+'/api/health');await offline(uiUrl);expect(listeners()).toEqual([])
}
function snapshot(){return JSON.parse(execFileSync(py,['-c',"import json,sys;from backend.tests.test_supplier_offers import snapshot;print(json.dumps(snapshot(sys.argv[1])))",database],{cwd:root,windowsHide:true,encoding:'utf8',stdio:['ignore','pipe','pipe']}))}
function legacy(s:any){return Object.fromEntries(Object.entries(s.rows).filter(([t])=>!['routing','routing_step','stock_create_receipt'].includes(t)))}
async function json(url='/api/routings'){const r=await fetch(uiUrl+url);expect(r.status).toBe(200);return r.json()}
function watch(page:Page){page.on('pageerror',e=>{throw e})}
async function newHeader(page:Page,product:number,revision:string){
 await page.getByRole('button',{name:'＋ 新增途程',exact:true}).click()
 await page.locator('#routing-product').selectOption(fixtures.products[product].id);await page.locator('#routing-revision').fill(revision)
 await page.locator('#routing-from').fill('2026-11-19');await page.locator('#routing-active').selectOption('true')
 const response=page.waitForResponse(r=>r.url().endsWith('/api/routings')&&r.request().method()==='POST')
 await page.getByRole('button',{name:'儲存表頭',exact:true}).click();const r=await response;expect(r.status()).toBe(201)
 await expect(page.getByRole('button',{name:'編輯表頭',exact:true})).toBeVisible();return r.json()
}
async function add(page:Page,sequence:string,operation:number,center:number,setup='',run=''){
 await page.getByRole('button',{name:'＋ 新增步驟',exact:true}).click()
 await page.locator('#routing-sequence').fill(sequence);await page.locator('#routing-operation').selectOption(fixtures.operations[operation].id)
 await page.locator('#routing-center').selectOption(fixtures.centers[center].id);await page.locator('#routing-setup').fill(setup);await page.locator('#routing-run').fill(run)
 const response=page.waitForResponse(r=>r.url().includes('/steps')&&r.request().method()==='POST')
 await page.getByRole('button',{name:'儲存步驟',exact:true}).click();const r=await response;expect(r.status()).toBe(201)
 await expect(page.getByRole('form',{name:'新增途程步驟',exact:true})).toHaveCount(0);return r.json()
}
async function open(page:Page,product:number,revision:string){
 await page.getByRole('button',{name:'開啟途程 '+fixtures.products[product].code+' '+revision,exact:true}).click()
 await expect(page.getByTestId('routing-revision-value')).toHaveText(revision)
}
async function order(page:Page){return page.locator('[data-testid^="routing-step-"]').evaluateAll(rows=>rows.map(r=>r.getAttribute('data-sequence')))}
test.beforeAll(async()=>{
 for(const url of [apiUrl,uiUrl]){let occupied=false;try{await fetch(url,{signal:AbortSignal.timeout(800)});occupied=true}catch{}if(occupied)throw Error('QA port occupied; no process stopped')}
 const output=execFileSync(py,[path.join(root,'backend/tests/seed_routing_browser.py'),database,task],{cwd:root,windowsHide:true,encoding:'utf8',stdio:['ignore','pipe','pipe']})
 writeFileSync(path.join(artifacts,'seed-output.txt'),output);fixtures=JSON.parse(readFileSync(path.join(task,'routing-browser-fixtures.json'),'utf8'))
 before=snapshot();await start();evidence.initialListeners=listeners();evidence.beforeBusinessRows=legacy(before)
})
test.afterAll(async()=>{
 for(const child of [...children].reverse())if(child.exitCode===null)await stop(child)
 const after=snapshot();expect(legacy(after)).toEqual(legacy(before))
 expect(after.rows.bom).toEqual(before.rows.bom);expect(after.rows.bom_component).toEqual(before.rows.bom_component)
 evidence.afterBusinessRows=legacy(after);evidence.finalSnapshot=after;evidence.qaServicesStopped=true
 writeFileSync(path.join(artifacts,'routing-browser-evidence.json'),JSON.stringify(evidence,null,2)+'\n')
})

test('desktop ordered/repeated steps, shared centers, exact minutes, independent BOM references and F5',async({page})=>{
 watch(page);await page.goto('/#routings');await expect(page.getByRole('heading',{name:'製程設定',exact:true})).toBeVisible()
 expect(await page.locator('#routing-from').getAttribute('type')).toBe('date')
 routeA=await newHeader(page,0,'R-A')
 const moved=await add(page,'30',0,0,'1.25',precise);await add(page,'10',1,1);await add(page,'20',2,0,'0');await add(page,'25',0,0)
 expect(await order(page)).toEqual(['10','20','25','30']);await expect(page.getByTestId('routing-step-'+moved.id).getByTestId('routing-run-value')).toContainText(precise+' 分鐘／SHEET · 張')
 routeB=await newHeader(page,1,'R-B');await add(page,'10',0,0,'-0.125')
 const other=await json('/api/routings/'+routeB.id)
 await open(page,0,'R-A');await page.getByRole('button',{name:'編輯步驟 30',exact:true}).click()
 await expect(page.getByRole('form',{name:'編輯途程步驟',exact:true})).toHaveCount(1)
 await expect(page.getByRole('button',{name:'編輯步驟 10',exact:true})).toBeDisabled();await expect(page.getByRole('button',{name:'＋ 新增步驟',exact:true})).toBeDisabled()
 await page.locator('#routing-sequence').fill('15');await page.getByRole('button',{name:'儲存步驟',exact:true}).click()
 await expect.poll(()=>order(page)).toEqual(['10','15','20','25'])
 expect((await json('/api/routings/'+routeA.id)).steps.find((s:any)=>s.id===moved.id).sequence_no).toBe('15');expect(await json('/api/routings/'+routeB.id)).toEqual(other)
 await page.locator('#routing-search').fill('CUT');const response=page.waitForResponse(r=>r.url().includes('/api/routings?q='));await page.getByRole('button',{name:'搜尋',exact:true}).click();await response
 await expect(page.getByRole('button',{name:/開啟途程/})).toHaveCount(2)
 const bomBefore=await json('/api/boms');await page.getByRole('link',{name:'MATERIAL-A · DRAFT ↗',exact:true}).click()
 await expect(page.getByTestId('bom-usage-1')).toContainText('2.75');await expect(page.getByTestId('bom-usage-2')).toContainText('0.5')
 await page.goto('/#routings?record='+routeA.id);await expect(page.getByTestId('routing-revision-value')).toHaveText('R-A')
 const saved=await json(),savedSnapshot=snapshot();await page.reload({waitUntil:'networkidle'})
 expect(await page.evaluate(()=>performance.getEntriesByType('navigation').map(e=>(e as PerformanceNavigationTiming).type))).toContain('reload')
 await expect(page.getByTestId('routing-revision-value')).toHaveText('R-A');expect(await order(page)).toEqual(['10','15','20','25'])
 expect(await json()).toEqual(saved);expect(snapshot()).toEqual(savedSnapshot);expect(await json('/api/boms')).toEqual(bomBefore)
 evidence.desktopOrderedStepsAndSharedCenters=true;evidence.preciseDurationRoundtrip=true;evidence.actualDocumentReload=true
})

test('header/step/operation Cancel sends zero writes; recoverable errors preserve drafts and eligible CRUD works',async({page})=>{
 watch(page);await page.goto('/#routings');await open(page,0,'R-A');const saved=snapshot();let mutations=0
 const count=(r:any)=>{if(['POST','PUT','DELETE','PATCH'].includes(r.method()))mutations++};page.on('request',count)
 await page.getByRole('button',{name:'編輯表頭',exact:true}).click();await page.locator('#routing-revision').fill('UNSAVED')
 await page.getByRole('button',{name:'取消',exact:true}).click();await expect(page.getByTestId('routing-revision-value')).toHaveText('R-A')
 await page.getByRole('button',{name:'編輯步驟 15',exact:true}).click();await page.locator('#routing-setup').fill('999')
 await page.getByRole('form',{name:'編輯途程步驟',exact:true}).getByRole('button',{name:'取消',exact:true}).click()
 await page.getByRole('button',{name:'＋ 新增步驟',exact:true}).click();await page.locator('#routing-sequence').fill('99')
 await page.getByRole('form',{name:'新增途程步驟',exact:true}).getByRole('button',{name:'取消',exact:true}).click()
 await page.getByRole('button',{name:'＋ 新增途程',exact:true}).click();await page.locator('#routing-revision').fill('UNSAVED')
 await page.getByRole('button',{name:'取消',exact:true}).click();await expect(page.locator('#routing-revision')).toHaveValue('')
 await page.getByRole('link',{name:'製程作業定義維護 ↗',exact:true}).click();await page.locator('#master-code').fill('UNSAVED')
 await page.getByRole('button',{name:'取消',exact:true}).click();await expect(page.locator('#master-code')).toHaveValue('')
 await page.getByRole('button',{name:'開啟 CUT',exact:true}).click();await page.locator('#master-instruction').fill('未儲存工法')
 await page.getByRole('button',{name:'取消',exact:true}).click();expect(mutations).toBe(0);expect(snapshot()).toEqual(saved);page.off('request',count)
 await page.getByRole('button',{name:'開啟 CUT',exact:true}).click();page.once('dialog',d=>d.accept());await page.getByRole('button',{name:'刪除製程作業',exact:true}).click()
 await expect(page.getByRole('alert')).toContainText('已被引用');await expectActionFeedback(page,'.editor-panel .form-actions')
 await page.getByRole('button',{name:'取消',exact:true}).click();await page.locator('#master-code').fill('TEMP-OP');await page.locator('#master-name').fill('臨時工法');await page.locator('#master-instruction').fill('新增說明')
 await page.getByRole('button',{name:'儲存製程作業',exact:true}).click();await expect(page.getByRole('status')).toContainText('已新增')
 await page.locator('#master-instruction').fill('修改工法');await page.getByRole('button',{name:'儲存變更',exact:true}).click();await expect(page.getByRole('status')).toContainText('變更已儲存')
 page.once('dialog',d=>d.accept());await page.getByRole('button',{name:'刪除製程作業',exact:true}).click();await expect(page.getByRole('status')).toContainText('已刪除')
 await page.goto('/#routings');await open(page,0,'R-A')
 page.once('dialog',d=>d.accept());await page.getByRole('button',{name:'刪除途程',exact:true}).click();await expect(page.getByRole('alert')).toContainText('仍有步驟');await expectActionFeedback(page,'section[aria-label="途程表頭"] .form-actions')
 await page.getByRole('button',{name:'編輯步驟 15',exact:true}).click();await page.locator('#routing-sequence').fill('10')
 await page.getByRole('button',{name:'儲存步驟',exact:true}).click();await expect(page.getByRole('alert')).toContainText('步序已存在');await expect(page.locator('#routing-sequence')).toHaveValue('10')
 await expectActionFeedback(page,'form[aria-label="編輯途程步驟"] .form-actions');await page.getByRole('form',{name:'編輯途程步驟',exact:true}).getByRole('button',{name:'取消',exact:true}).click()
 const temporary=await newHeader(page,0,'DELETE-ME');await add(page,'0',0,0)
 await page.getByRole('button',{name:'編輯表頭',exact:true}).click();await page.locator('#routing-revision').fill('DELETE-UPDATED');await page.getByRole('button',{name:'儲存表頭',exact:true}).click();await expect(page.getByTestId('routing-revision-value')).toHaveText('DELETE-UPDATED')
 page.once('dialog',d=>d.accept());await page.getByRole('button',{name:'刪除步驟 0',exact:true}).click();await expect(page.locator('[data-testid^="routing-step-"]')).toHaveCount(0)
 page.once('dialog',d=>d.accept());await page.getByRole('button',{name:'刪除途程',exact:true}).click();await expect(page.locator('#routing-revision')).toHaveValue('')
 expect((await json()).some((r:any)=>r.id===temporary.id)).toBe(false)
 evidence.desktopCancelMutations=mutations;evidence.recoverableFeedbackAndReferenceGuards=true;evidence.operationMasterCrud=true
 evidence.beforeRestart={rows:await json(),snapshot:snapshot(),listeners:listeners()}
})

test('complete QA restart preserves records; mobile exact sequence editing, Cancel and existing BOM/purchase navigation',async({page})=>{
 watch(page);await stop(launcher);evidence.offlineListeners=listeners();await start()
 const next=listeners();expect(next).toHaveLength(2)
 expect(next.every((r:any)=>r.LocalAddress==='127.0.0.1'&&!evidence.beforeRestart.listeners.some((p:any)=>p.OwningProcess===r.OwningProcess))).toBe(true)
 expect(await json()).toEqual(evidence.beforeRestart.rows);expect(snapshot()).toEqual(evidence.beforeRestart.snapshot)
 await page.setViewportSize({width:390,height:844});await page.goto('/#routings?record='+routeA.id);await expect(page.getByTestId('routing-revision-value')).toHaveText('R-A')
 expect(await order(page)).toEqual(['10','15','20','25']);expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true)
 const cancelSnapshot=snapshot();let mutations=0;const count=(r:any)=>{if(['POST','PUT','DELETE','PATCH'].includes(r.method()))mutations++};page.on('request',count)
 await page.getByRole('button',{name:'編輯步驟 15',exact:true}).click();await page.locator('#routing-run').fill('未儲存')
 const form=page.getByRole('form',{name:'編輯途程步驟',exact:true});await expect(form.getByRole('button',{name:'取消',exact:true})).toBeVisible();await form.getByRole('button',{name:'取消',exact:true}).click()
 await page.getByRole('button',{name:'編輯表頭',exact:true}).click();await page.locator('#routing-revision').fill('手機未儲存');await page.getByRole('button',{name:'取消',exact:true}).click()
 expect(mutations).toBe(0);expect(snapshot()).toEqual(cancelSnapshot);page.off('request',count)
 await page.getByRole('button',{name:'編輯步驟 15',exact:true}).click();await page.locator('#routing-sequence').fill('9007199254740993');await page.getByRole('button',{name:'儲存步驟',exact:true}).click()
 await expect.poll(()=>order(page)).toEqual(['10','20','25','9007199254740993'])
 expect((await json('/api/routings/'+routeA.id)).steps.at(-1).sequence_no).toBe('9007199254740993');expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true)
 page.once('dialog',d=>d.accept());await page.getByRole('button',{name:'刪除途程',exact:true}).click();await expectActionFeedback(page,'section[aria-label="途程表頭"] .form-actions')
 await page.getByRole('button',{name:'製程作業定義',exact:true}).filter({visible:true}).click();await page.locator('#master-code').fill('手機未儲存')
 await page.getByRole('button',{name:'取消',exact:true}).click();await expect(page.locator('#master-code')).toHaveValue('')
 await page.getByRole('button',{name:'採購管理',exact:false}).first().click();await page.getByRole('button',{name:'未到貨採購查詢',exact:true}).filter({visible:true}).click()
 await expect(page.getByTestId('oq-order-count')).toHaveText('2');expect(await json('/api/purchase-outstanding')).toEqual(fixtures.inquiry)
 await page.getByRole('button',{name:'製造生產',exact:false}).first().click();await expect(page.getByRole('heading',{name:'產品 BOM 用料表',exact:true})).toBeVisible()
 await page.getByRole('button',{name:'製程設定',exact:true}).filter({visible:true}).click();await open(page,1,'R-B');await expect(page.locator('[data-testid^="routing-step-"]')).toHaveCount(1)
 expect(await json('/api/boms')).toEqual(fixtures.boms);expect(await json('/api/stock-positions')).toEqual(fixtures.stock);expect(legacy(snapshot())).toEqual(legacy(before))
 evidence.mobileCancelMutations=mutations;evidence.mobileExactIntegerBeyondJsSafeRange=true;evidence.mobileNoOverflow=true
 evidence.afterRestart={listeners:next,rows:await json(),snapshot:snapshot(),bomAndPurchaseUnchanged:true}
})
