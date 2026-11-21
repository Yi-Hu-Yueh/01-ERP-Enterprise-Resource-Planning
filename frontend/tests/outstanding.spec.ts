import {test,expect,type Page} from '@playwright/test'
import {spawn,execFileSync,type ChildProcess} from 'node:child_process'
import {mkdirSync,createWriteStream,writeFileSync,readFileSync} from 'node:fs'
import path from 'node:path'
import {fileURLToPath} from 'node:url'
import {randomUUID} from 'node:crypto'
import {expectActionFeedback} from './feedback-assertions'

const root=path.resolve(fileURLToPath(new URL('../../',import.meta.url)))
const artifacts=path.resolve(root,process.env.ERP_TEST_ARTIFACTS||'artifacts/manual-round10/browser')
const task=path.dirname(artifacts)
mkdirSync(artifacts,{recursive:true})
const database=path.join(task,'browser-inquiry-'+randomUUID()+'.sqlite')
const py=path.join(root,'.venv','Scripts','python.exe'),apiUrl='http://127.0.0.1:8018',uiUrl='http://127.0.0.1:5181'
let launcher:ChildProcess,fixtures:any,before:any
const children:ChildProcess[]=[],evidence:Record<string,any>={isolated:true,database,urls:{apiUrl,uiUrl},generatedScreenshots:0,mutationRequests:0}
test.use({baseURL:uiUrl,screenshot:'off',trace:'off'})
test.describe.configure({mode:'serial'})
async function ready(url:string){await expect.poll(async()=>{try{return(await fetch(url,{signal:AbortSignal.timeout(1000)})).status}catch{return 0}},{timeout:40_000}).toBe(200)}
async function offline(url:string){await expect.poll(async()=>{try{return(await fetch(url,{signal:AbortSignal.timeout(1000)})).status}catch{return 0}},{timeout:20_000}).toBe(0)}
async function start(){launcher=spawn(py,['-u','scripts/dev.py','--backend-port','8018','--frontend-port','5181','--database',database],{cwd:root,windowsHide:true,stdio:['ignore','pipe','pipe']});children.push(launcher);launcher.stdout?.pipe(createWriteStream(path.join(artifacts,'launcher.log'),{flags:'a'}));launcher.stderr?.pipe(createWriteStream(path.join(artifacts,'launcher-error.log'),{flags:'a'}));await ready(apiUrl+'/api/health');await ready(uiUrl);expect(launcher.exitCode).toBeNull()}
async function stop(child:ChildProcess){if(child.exitCode!==null)throw Error('Owned launcher exited unexpectedly');execFileSync('taskkill.exe',['/PID',String(child.pid),'/T','/F'],{windowsHide:true,stdio:'pipe'});await new Promise<void>(resolve=>{if(child.exitCode!==null)resolve();else child.once('exit',()=>resolve())});await offline(apiUrl+'/api/health');await offline(uiUrl)}
function listeners(){return JSON.parse(execFileSync('powershell.exe',['-NoProfile','-Command',"@(Get-NetTCPConnection -State Listen | Where-Object LocalPort -in 8018,5181 | Select-Object LocalAddress,LocalPort,OwningProcess) | ConvertTo-Json -Compress"],{windowsHide:true,encoding:'utf8'}))}
function snapshot(){return JSON.parse(execFileSync(py,['-c',"import json,sys;from backend.tests.test_supplier_offers import snapshot;print(json.dumps(snapshot(sys.argv[1])))",database],{cwd:root,windowsHide:true,encoding:'utf8'}))}
async function json(url='/api/purchase-outstanding'){const response=await fetch(uiUrl+url);expect(response.status).toBe(200);return response.json()}
function readOnly(page:Page){page.on('request',r=>{if(['POST','PUT','PATCH','DELETE'].includes(r.method()))evidence.mutationRequests++});page.on('pageerror',e=>{throw e})}
async function search(page:Page,q:string){await page.locator('#oq-search').fill(q);const response=page.waitForResponse(r=>r.url().includes('/api/purchase-outstanding?'));await page.getByRole('button',{name:'查詢',exact:true}).click();await response;await expect(page.getByRole('status')).toHaveCount(0)}
async function clear(page:Page){const response=page.waitForResponse(r=>r.url().includes('/api/purchase-outstanding?'));await page.getByRole('button',{name:'清除條件',exact:true}).click();await response;await expect(page.getByTestId('oq-order-count')).toHaveText('4')}
test.beforeAll(async()=>{
 for(const url of [apiUrl,uiUrl]){let occupied=false;try{await fetch(url,{signal:AbortSignal.timeout(800)});occupied=true}catch{}if(occupied)throw Error('QA port occupied; nothing stopped')}
 const output=execFileSync(py,[path.join(root,'backend/tests/seed_outstanding_browser.py'),database,task],{cwd:root,windowsHide:true,encoding:'utf8'});writeFileSync(path.join(artifacts,'seed-output.txt'),output)
 fixtures=JSON.parse(readFileSync(path.join(task,'browser-fixtures.json'),'utf8'));before=snapshot();await start();evidence.initialListeners=listeners();evidence.initialSnapshot=before
})
test.afterAll(async()=>{for(const child of [...children].reverse())if(child.exitCode===null)await stop(child);evidence.finalSnapshot=snapshot();expect(evidence.finalSnapshot).toEqual(before);expect(evidence.mutationRequests).toBe(0);evidence.qaServicesStopped=true;writeFileSync(path.join(artifacts,'inquiry-browser-evidence.json'),JSON.stringify(evidence,null,2)+'\n')})

test('desktop filters, all multi-line details and direct source navigation are read-only',async({page})=>{
 readOnly(page);await page.goto('/#purchase-outstanding');await expect(page.getByRole('heading',{name:'未到貨採購查詢',exact:true})).toBeVisible();await expect(page.getByTestId('oq-order-count')).toHaveText('4')
 expect(await page.locator('#oq-from').getAttribute('type')).toBe('date');expect(await page.locator('#oq-to').getAttribute('type')).toBe('date')
 await page.locator('#oq-item').selectOption(fixtures.raw.id);await search(page,'MULTI');await expect(page.getByTestId('oq-order-count')).toHaveText('1');await page.getByRole('button',{name:'查看全部明細 · MULTI',exact:true}).click();const multi=page.getByTestId('oq-order-MULTI');await expect(multi.getByTestId('oq-line-1').getByTestId('oq-line-quantities')).toHaveText('2 / 0.5 / 1.5');await expect(multi.getByTestId('oq-line-2').getByTestId('oq-line-quantities')).toHaveText('4 / 1 / 3');await expect(multi.getByTestId('oq-order-sum')).toHaveText('26')
 await clear(page);await page.locator('#oq-supplier').selectOption(fixtures.otherSupplier.id);await search(page,'');await expect(page.getByTestId('oq-order-count')).toHaveText('1');await expect(page.getByTestId('oq-order-SUPPLIER-OTHER')).toBeVisible()
 await clear(page);await page.locator('#oq-from').fill('2026-11-20');await page.locator('#oq-to').fill('2026-11-20');await search(page,'');await expect(page.getByTestId('oq-order-count')).toHaveText('1');await expect(page.getByTestId('oq-order-MULTI')).toBeVisible();await clear(page)
 await search(page,'PO-TEST-001');await page.getByRole('button',{name:'查看全部明細 · PO-TEST-001',exact:true}).click();await page.getByRole('link',{name:'查看原採購單 · PO-TEST-001',exact:true}).click();await expect(page.getByTestId('po-total')).toHaveText('156');await expect(page.locator('section[aria-label="採購表頭"] h2')).toHaveText('PO-TEST-001')
 await page.goto('/#purchase-outstanding');await search(page,'PO-TEST-001');await page.getByRole('button',{name:'查看全部明細 · PO-TEST-001',exact:true}).click();await page.getByRole('link',{name:'RCV-TEST-001 · 明細 1',exact:true}).click();await expect(page.getByTestId('rc-state')).toHaveText('已過帳 · 唯讀');await expect(page.getByTestId('rc-onhand-1')).toHaveText('4');await expect(page.getByTestId('rc-quantities-1')).toHaveText('12 / 10 / 2')
 expect(snapshot()).toEqual(before);evidence.desktopFiltersDetailsAndSourceLinks=true
})

test('gross/returns/retained arithmetic, empty/error/clear and genuine document refresh',async({page})=>{
 readOnly(page);await page.goto('/#purchase-outstanding');await search(page,'PO-TEST-001');await page.getByRole('button',{name:'查看全部明細 · PO-TEST-001',exact:true}).click();let section=page.getByTestId('oq-order-PO-TEST-001');await expect(section.getByTestId('oq-line-quantities')).toHaveText('12 / 10 / 2');await expect(section.getByTestId('oq-line-returned')).toHaveText('6');await expect(section.getByTestId('oq-line-net')).toHaveText('4');await expect(page.getByRole('button',{name:/儲存|過帳|刪除/})).toHaveCount(0)
 const previous=await json();await page.reload({waitUntil:'networkidle'});expect(await page.evaluate(()=>performance.getEntriesByType('navigation').map(e=>(e as PerformanceNavigationTiming).type))).toContain('reload');expect(await json()).toEqual(previous)
 await search(page,'NO-MATCH');await expect(page.getByRole('heading',{name:'沒有符合條件的採購單',exact:true})).toBeVisible();await clear(page);await page.locator('#oq-from').fill('2026-11-21');await page.locator('#oq-to').fill('2026-11-20');await search(page,'');await expect(page.getByRole('alert')).toContainText('起始日期');await expectActionFeedback(page,'section[aria-label="採購查詢條件"] .form-actions');await clear(page)
 await search(page,'PO-TEST-002');await page.getByRole('button',{name:'查看全部明細 · PO-TEST-002',exact:true}).click();section=page.getByTestId('oq-order-PO-TEST-002');await expect(section.getByTestId('oq-line-quantities')).toHaveText('5 / 0 / 5');await expect(section.getByTestId('oq-order-sum')).toHaveText('36.25');expect(snapshot()).toEqual(before)
 evidence.beforeRestart={query:await json(),snapshot:snapshot(),listeners:listeners()};evidence.realDocumentReload=true;evidence.grossOutstandingUnaffectedByReturns=true
})

test('both QA services actually stop/restart, mobile inquiry/navigation preserves all rows',async({page})=>{
 readOnly(page);await stop(launcher);evidence.bothQaServicesObservedOffline=true;await start();const next=listeners();expect(next).toHaveLength(2);expect(next.every((r:any)=>r.LocalAddress==='127.0.0.1'&&!evidence.beforeRestart.listeners.some((p:any)=>p.OwningProcess===r.OwningProcess))).toBe(true);expect(await json()).toEqual(evidence.beforeRestart.query);expect(snapshot()).toEqual(before)
 await page.setViewportSize({width:390,height:844});await page.goto('/#purchase-outstanding');await expect(page.getByTestId('oq-order-count')).toHaveText('4');await search(page,'PO-TEST-001');await page.getByRole('button',{name:'查看全部明細 · PO-TEST-001',exact:true}).click();await expect(page.getByTestId('oq-line-quantities')).toHaveText('12 / 10 / 2');await expect(page.getByTestId('oq-line-returned')).toHaveText('6');await expect(page.getByTestId('oq-line-net')).toHaveText('4');expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true)
 await clear(page);await page.locator('#oq-only').check();await search(page,'');await expect(page.getByTestId('oq-order-count')).toHaveText('4');await page.getByRole('button',{name:'採購訂單管理',exact:true}).filter({visible:true}).click();await expect(page.getByRole('heading',{name:'採購訂單管理',exact:true})).toBeVisible();await page.getByRole('button',{name:'未到貨採購查詢',exact:true}).filter({visible:true}).click();await expect(page.getByRole('heading',{name:'未到貨採購查詢',exact:true})).toBeVisible()
 expect(snapshot()).toEqual(before);evidence.afterRestart={listeners:next,query:await json(),snapshot:snapshot(),mobileNoHorizontalOverflow:true};evidence.mobileReadOnly=true
})
