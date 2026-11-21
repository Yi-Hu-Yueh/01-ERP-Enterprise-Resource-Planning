import {test,expect,type Page} from '@playwright/test'
import {spawn,execFileSync,type ChildProcess} from 'node:child_process'
import {mkdirSync,createWriteStream,writeFileSync,readFileSync} from 'node:fs'
import path from 'node:path'
import {fileURLToPath} from 'node:url'
import {randomUUID} from 'node:crypto'

const root=path.resolve(fileURLToPath(new URL('../../',import.meta.url)))
const artifacts=path.resolve(root,process.env.ERP_TEST_ARTIFACTS||'artifacts/manual-round14/browser/results'),task=path.dirname(artifacts)
mkdirSync(artifacts,{recursive:true})
const database=path.join(task,'browser-mrp-'+randomUUID()+'.sqlite')
const py=path.join(root,'.venv','Scripts','python.exe'),apiUrl='http://127.0.0.1:8022',uiUrl='http://127.0.0.1:5185'
let launcher:ChildProcess,fixtures:any
const evidence:any={desktopOnly:true,mobileTests:0,pageErrors:[],businessMutationRequests:[],previewRequests:0,stops:[]}
test.use({baseURL:uiUrl,viewport:{width:1440,height:1100},isMobile:false,screenshot:'off',trace:'off'})
test.describe.configure({mode:'serial'})
async function ready(url:string){await expect.poll(async()=>{try{return(await fetch(url,{signal:AbortSignal.timeout(1000)})).status}catch{return 0}},{timeout:40_000}).toBe(200)}
async function offline(url:string){await expect.poll(async()=>{try{return(await fetch(url,{signal:AbortSignal.timeout(1000)})).status}catch{return 0}},{timeout:20_000}).toBe(0)}
function ps(script:string){return JSON.parse(execFileSync('powershell.exe',['-NoProfile','-Command',script],{windowsHide:true,encoding:'utf8',stdio:['ignore','pipe','pipe']}))}
function listeners(){return ps('ConvertTo-Json -InputObject @(Get-NetTCPConnection -State Listen | Where-Object LocalPort -in 8022,5185 | Select-Object LocalAddress,LocalPort,OwningProcess) -Compress')}
function ownership(child:ChildProcess){
 const pid=Number(child.pid)
 if(!Number.isInteger(pid))throw Error('No QA launcher PID')
 const tree=ps('$qaProcesses=Get-CimInstance Win32_Process; $qaIds=@('+pid+'); do {$qaChildren=@($qaProcesses | Where-Object {$_.ParentProcessId -in $qaIds -and $_.ProcessId -notin $qaIds} | Select-Object -ExpandProperty ProcessId); $qaIds+=$qaChildren}while($qaChildren.Count -gt 0); ConvertTo-Json -InputObject @($qaProcesses | Where-Object ProcessId -in $qaIds | Select-Object ProcessId,ParentProcessId,ExecutablePath,CommandLine,CreationDate) -Depth 5 -Compress')
 const parent=tree.find((p:any)=>p.ProcessId===pid)
 if(!parent||String(parent.ExecutablePath).toLowerCase()!==py.toLowerCase()||!String(parent.CommandLine).toLowerCase().includes(database.toLowerCase()))throw Error('QA process ownership mismatch')
 const sockets=listeners(),ids=new Set(tree.map((p:any)=>p.ProcessId))
 if(sockets.length!==2||sockets.some((s:any)=>s.LocalAddress!=='127.0.0.1'||!ids.has(s.OwningProcess)))throw Error('QA listener ownership mismatch')
 return {rootPid:pid,tree,listeners:sockets}
}
async function start(){
 launcher=spawn(py,['-u','scripts/dev.py','--backend-port','8022','--frontend-port','5185','--database',database],{cwd:root,windowsHide:true,stdio:['ignore','pipe','pipe']})
 launcher.stdout?.pipe(createWriteStream(path.join(artifacts,'launcher.log'),{flags:'a'}));launcher.stderr?.pipe(createWriteStream(path.join(artifacts,'launcher-error.log'),{flags:'a'}))
 await ready(apiUrl+'/api/health');await ready(uiUrl);return ownership(launcher)
}
async function stop(){
 const owned=ownership(launcher)
 execFileSync('taskkill.exe',['/PID',String(launcher.pid),'/T','/F'],{windowsHide:true,stdio:'pipe'})
 await new Promise<void>(resolve=>{if(launcher.exitCode!==null||launcher.signalCode!==null)resolve();else launcher.once('exit',()=>resolve())})
 await offline(apiUrl+'/api/health');await offline(uiUrl);expect(listeners()).toEqual([])
 evidence.stops.push({before:owned,afterListeners:[]})
}
function snapshot(){return JSON.parse(execFileSync(py,['-c','import json,sys;from backend.tests.test_supplier_offers import snapshot;print(json.dumps(snapshot(sys.argv[1])))',database],{cwd:root,windowsHide:true,encoding:'utf8',stdio:['ignore','pipe','pipe']}))}
function watch(page:Page){
 page.on('pageerror',e=>evidence.pageErrors.push(e.message))
 page.on('request',r=>{if(['POST','PUT','DELETE','PATCH'].includes(r.method())){
  if(r.method()==='POST'&&new URL(r.url()).pathname==='/api/mrp/preview')evidence.previewRequests++
  else evidence.businessMutationRequests.push({url:r.url(),method:r.method()})
 }})
}
async function run(page:Page){
 const response=page.waitForResponse(r=>r.url().endsWith('/api/mrp/preview')&&r.request().method()==='POST')
 await page.getByRole('button',{name:'重新試算',exact:true}).click()
 const r=await response;expect(r.status()).toBe(200)
 await expect(page.getByRole('status')).toContainText('試算完成')
 return r.json()
}
function aggregate(page:Page,index=0){return page.getByTestId('mrp-material-'+fixtures.raws[index].code)}
async function select(page:Page,index:number){await page.getByRole('checkbox',{name:'選取工單 '+fixtures.orders[index].document_no,exact:true}).check()}
async function warehouse(page:Page,index=0){await page.getByRole('checkbox',{name:'選取倉庫 '+(index?fixtures.warehouse2:fixtures.warehouse).code,exact:true}).check()}
async function clear(page:Page){await page.getByRole('button',{name:'取消／清除選取',exact:true}).click();await expect(page.getByRole('status')).toContainText('未修改任何業務資料')}
function editBody(row:any,patch:any){return {...Object.fromEntries(['document_no','product_id','planned_qty_base','planned_start','planned_finish','note','bom_id','routing_id','row_version'].map(k=>[k,row[k]])),bom_version:row.bom?.row_version??null,routing_version:row.routing?.row_version??null,...patch}}

test.beforeAll(async()=>{
 expect(listeners()).toEqual([])
 const output=execFileSync(py,[path.join(root,'backend/tests/seed_mrp_browser.py'),database,task],{cwd:root,windowsHide:true,encoding:'utf8',stdio:['ignore','pipe','pipe']})
 writeFileSync(path.join(artifacts,'seed-output.txt'),output)
 fixtures=JSON.parse(readFileSync(path.join(task,'mrp-browser-fixtures.json'),'utf8'))
 evidence.initialOwnership=await start()
})
test.afterEach(()=>{expect(evidence.pageErrors).toEqual([]);expect(evidence.businessMutationRequests).toEqual([])})
test.afterAll(async()=>{
 try{if(launcher&&launcher.exitCode===null&&launcher.signalCode===null)await stop()}
 finally{writeFileSync(path.join(artifacts,'desktop-mrp-evidence.json'),JSON.stringify(evidence,null,2))}
})

test('Desktop one/multiple orders, common stock once, provenance, F5 and clear without business writes',async({page})=>{
 watch(page);const before=snapshot();await page.goto('/#mrp-preview')
 await expect(page.getByRole('heading',{name:'智慧物料需求與缺料分析',exact:true})).toBeVisible()
 await expect(page.getByRole('checkbox',{checked:true})).toHaveCount(0)
 await page.getByRole('button',{name:'重新試算',exact:true}).click()
 await expect(page.getByRole('alert')).toContainText('請至少選取')
 await select(page,0);await warehouse(page)
 await run(page)
 await expect(aggregate(page).getByTestId('gross')).toContainText('6.875 ')
 await expect(aggregate(page).getByTestId('shortage')).toContainText('2.875')
 await select(page,1);await expect(page.getByRole('region',{name:'共同物料需求分析'})).toHaveCount(0)
 await run(page)
 await expect(aggregate(page).getByTestId('gross')).toContainText('11.5625')
 await expect(aggregate(page).getByTestId('physical')).toContainText('4 ')
 await expect(aggregate(page).getByTestId('shortage')).toContainText('7.5625')
 await expect(aggregate(page,1).getByTestId('gross')).toContainText('1.25')
 await expect(page.getByTestId('mrp-order-WO-TEST-002')).toContainText('未選取途程')
 await expect(page.getByTestId('mrp-order-WO-TEST-001')).toContainText('2.5 × 2.75 ÷ 1')
 const feedback=page.locator('[data-feedback-group]'),box=await feedback.boundingBox()
 expect(box).not.toBeNull();expect(box!.y).toBeGreaterThanOrEqual(0);expect(box!.y+box!.height).toBeLessThanOrEqual(1101)
 await page.reload();await expect(page.getByRole('checkbox',{checked:true})).toHaveCount(3)
 await run(page);await expect(aggregate(page).getByTestId('shortage')).toContainText('7.5625')
 await page.screenshot({path:path.join(artifacts,'desktop-mrp.png'),fullPage:true})
 await clear(page);await expect(page.getByRole('checkbox',{checked:true})).toHaveCount(0)
 expect(snapshot()).toEqual(before);evidence.reference={demand:'11.5625',stock:'4',shortage:'7.5625',f5:true,zeroBusinessWrites:true}
})

test('Desktop missing BOM stays unknown, search preserves selection and explicit combined warehouse scope',async({page})=>{
 watch(page);const before=snapshot();await page.goto('/#mrp-preview')
 await page.getByRole('checkbox',{name:'選取工單 WO-MISSING-BOM',exact:true}).check();await select(page,0);await warehouse(page)
 await page.locator('#mrp-search').fill('WO-TEST-001');await run(page)
 await expect(page.getByTestId('mrp-order-WO-MISSING-BOM')).toContainText('無法計算需求，不能視為零')
 await expect(aggregate(page).getByTestId('gross')).toHaveText('未定')
 await expect(aggregate(page).getByTestId('shortage')).toHaveText('未定')
 await clear(page);await select(page,0);await select(page,1);await warehouse(page,1);await run(page)
 await expect(aggregate(page).getByTestId('physical')).toContainText('0 ')
 await expect(aggregate(page).getByTestId('shortage')).toContainText('11.5625')
 await warehouse(page);await run(page)
 await expect(aggregate(page).getByTestId('physical')).toContainText('4 ')
 await expect(aggregate(page).getByTestId('shortage')).toContainText('7.5625')
 expect(snapshot()).toEqual(before);evidence.explicitWarehouses=true;evidence.missingBomUnknown=true
})

test('Desktop stale version/recalculate and true QA frontend/backend stop/restart with persisted zero-write results',async({page,request})=>{
 watch(page);await page.goto('/#mrp-preview');await clear(page);await select(page,0);await select(page,1);await warehouse(page);await run(page)
 const row=fixtures.orders[0]
 const changed=await request.put(apiUrl+'/api/work-orders/'+row.id,{data:editBody(row,{planned_qty_base:'5'})})
 expect(changed.status()).toBe(200)
 const before=snapshot()
 await page.getByRole('button',{name:'重新試算',exact:true}).click();await expect(page.getByRole('alert')).toContainText('已更新')
 await expect(page.getByRole('region',{name:'共同物料需求分析'})).toHaveCount(0)
 await page.getByRole('button',{name:'重新讀取來源',exact:true}).click();await expect(page.getByRole('status')).toContainText('來源已重新讀取')
 await run(page);await expect(aggregate(page).getByTestId('gross')).toContainText('18.4375')
 await expect(aggregate(page).getByTestId('shortage')).toContainText('14.4375')
 const old=ownership(launcher);await stop();const restarted=await start()
 expect(restarted.rootPid).not.toBe(old.rootPid)
 expect(restarted.listeners.every((s:any)=>!old.listeners.some((p:any)=>p.OwningProcess===s.OwningProcess))).toBe(true)
 expect(snapshot()).toEqual(before)
 await page.reload();await run(page);await expect(aggregate(page).getByTestId('gross')).toContainText('18.4375')
 expect(snapshot()).toEqual(before);evidence.restart={old,restarted,zeroListenersObserved:true,persistedResult:'18.4375',zeroBusinessWrites:true}
})
