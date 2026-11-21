import {test,expect,type Page} from '@playwright/test'
import {spawn,execFileSync,type ChildProcess} from 'node:child_process'
import {mkdirSync,readFileSync,writeFileSync,createWriteStream} from 'node:fs'
import path from 'node:path'
import {fileURLToPath} from 'node:url'
const root=path.resolve(fileURLToPath(new URL('../../',import.meta.url)))
const task=path.resolve(root,process.env.ERP_SALES_QA_DIR||'artifacts/round19-2026-11-20T04-45-52-060Z')
const out=path.join(task,'desktop');mkdirSync(out,{recursive:true})
const fixtures=JSON.parse(readFileSync(path.join(task,'sales-fixtures.json'),'utf8'))
const py=path.join(root,'.venv/Scripts/python.exe'),db=path.join(task,'sales-browser.sqlite'),api='http://127.0.0.1:8034',ui='http://127.0.0.1:5197'
const ports=[8034,5197]
let child:ChildProcess,order:any,other:any,baseline:any,originalRows:any,oldPids:number[]=[]
const runId=Date.now().toString()
const evidence:any={mobileTests:0,ownerMutations:0,pageErrors:[],cancelMutationCounts:[],stops:[]}
test.use({baseURL:ui,viewport:{width:1440,height:1100},isMobile:false,trace:'off',screenshot:'off'})
test.describe.configure({mode:'serial'})
function ps(script:string){return JSON.parse(execFileSync('powershell.exe',['-NoProfile','-Command',script],{windowsHide:true,encoding:'utf8',stdio:['ignore','pipe','pipe']}))}
function listeners(){return ps('ConvertTo-Json -InputObject @(Get-NetTCPConnection -State Listen | Where-Object LocalPort -in 8034,5197 | Select-Object LocalAddress,LocalPort,OwningProcess) -Compress')}
async function ready(url:string){await expect.poll(async()=>{try{return(await fetch(url,{signal:AbortSignal.timeout(1000)})).status}catch{return 0}},{timeout:45_000}).toBe(200)}
async function start(){expect(listeners()).toEqual([]);child=spawn(py,['-u','scripts/dev.py','--database',db,'--backend-port','8034','--frontend-port','5197'],{cwd:root,windowsHide:true,stdio:['ignore','pipe','pipe']});child.stdout?.pipe(createWriteStream(path.join(out,'runtime.log'),{flags:'a'}));child.stderr?.pipe(createWriteStream(path.join(out,'runtime-error.log'),{flags:'a'}));await ready(api+'/api/health');await ready(ui)}
async function stop(){
  const pid=child.pid;if(!pid||child.exitCode!==null)return
  const tree=ps('$all=@(Get-CimInstance Win32_Process);$ids=[System.Collections.Generic.HashSet[int]]::new();$queue=[System.Collections.Generic.Queue[int]]::new();$queue.Enqueue('+pid+');while($queue.Count){$id=$queue.Dequeue();if($ids.Add($id)){foreach($p in @($all|Where-Object ParentProcessId -eq $id)){$queue.Enqueue([int]$p.ProcessId)}}};ConvertTo-Json -InputObject @($all|Where-Object {$ids.Contains([int]$_.ProcessId)}|Select-Object ProcessId,ParentProcessId,ExecutablePath,CommandLine) -Compress')
  const parent=tree.find((p:any)=>p.ProcessId===pid);expect(parent.ExecutablePath.toLowerCase()).toBe(py.toLowerCase());expect(parent.CommandLine).toContain('scripts/dev.py');expect(parent.CommandLine).toContain(db)
  const sockets=listeners(),ids=new Set(tree.map((p:any)=>p.ProcessId));expect(sockets).toHaveLength(2);expect(sockets.every((s:any)=>s.LocalAddress==='127.0.0.1'&&ids.has(s.OwningProcess))).toBe(true)
  execFileSync('taskkill.exe',['/PID',String(pid),'/T','/F'],{windowsHide:true,stdio:'pipe'});await new Promise<void>(resolve=>{if(child.exitCode!==null||child.signalCode!==null)resolve();else child.once('exit',()=>resolve())})
  await expect.poll(listeners).toEqual([]);evidence.stops.push({before:sockets,after:[]})
}
async function json(url:string){const r=await fetch(api+url);expect(r.status).toBe(200);return r.json()}
function rows(){return JSON.parse(execFileSync(py,['-c','import json,sys;from backend.tests.test_supplier_offers import snapshot;print(json.dumps(snapshot(sys.argv[1])["rows"]))',db],{cwd:root,windowsHide:true,encoding:'utf8',stdio:['ignore','pipe','pipe']}))}
async function preserved(){for(const [k,v] of Object.entries(baseline))expect(await json('/api/'+k),k).toEqual(v);const after=rows();for(const [k,v]of Object.entries(originalRows))if(!k.startsWith('sales_'))expect(after[k],k).toEqual(v)}
function watch(page:Page){page.on('pageerror',e=>evidence.pageErrors.push(e.message))}
async function open(page:Page,o=order){await page.goto('/#sales-orders?record='+o.id);await expect(page.getByRole('heading',{name:o.document_no,exact:true})).toBeVisible();await expect(page.getByTestId('so-total')).toHaveText(o.total_amount)}
async function header(page:Page,no:string,index=0){await page.getByRole('button',{name:'＋ 新增銷售單',exact:true}).click();await page.getByLabel('銷售單號 *',{exact:true}).fill(no);await page.getByLabel('客戶 *',{exact:true}).selectOption(fixtures.customers[index].id);await page.getByLabel('銷售日期 *',{exact:true}).fill('2026-11-20');await page.getByLabel('備註',{exact:true}).fill('第十九輪獨立桌面驗收');await page.getByRole('button',{name:'儲存變更',exact:true}).click();await expect(page.getByTestId('so-total')).toHaveText('0');return(await json('/api/sales-orders?q='+no))[0]}
async function add(page:Page,index:number,qty:string,price:string){await page.getByRole('button',{name:'＋ 新增明細',exact:true}).click();await page.getByLabel('成品 *',{exact:true}).selectOption(fixtures.products[index].id);await page.getByLabel('數量 *',{exact:true}).fill(qty);await page.getByLabel('單價 *',{exact:true}).fill(price);await page.getByRole('button',{name:'儲存明細',exact:true}).click();await expect(page.getByRole('form',{name:'新增銷售明細'})).toHaveCount(0)}
async function cancelWithoutMutation(page:Page,action:()=>Promise<void>){const mutations:string[]=[];const listener=(r:any)=>{if(!['GET','HEAD'].includes(r.method()))mutations.push(r.method()+' '+r.url())};page.on('request',listener);await action();await page.waitForTimeout(200);page.off('request',listener);expect(mutations).toEqual([]);evidence.cancelMutationCounts.push(mutations.length)}
test.beforeAll(async()=>{await start();baseline=Object.fromEntries(await Promise.all(Object.keys(fixtures.baseline).map(async k=>[k,await json('/api/'+k)])));originalRows=rows();oldPids=listeners().map((s:any)=>s.OwningProcess)})
test.afterAll(async()=>{if(child&&child.exitCode===null)await stop();writeFileSync(path.join(out,'sales-desktop-evidence.json'),JSON.stringify(evidence,null,2)+'\n')})

test('A — real desktop header, two independent details, exact 68.125 and stock separation',async({page})=>{
  watch(page);await page.goto('/#raw-materials');await page.getByRole('button',{name:'銷售管理',exact:false}).first().click();await expect(page.getByRole('heading',{name:'銷售訂單管理',exact:true})).toBeVisible()
  order=await header(page,'SO-QA-DESKTOP-A-'+runId);await add(page,0,'1.25','40');await expect(page.getByTestId('so-amount-1')).toHaveText('50');await add(page,1,'2.5','7.25')
  await expect(page.getByTestId('so-amount-2')).toHaveText('18.125');await expect(page.getByTestId('so-total')).toHaveText('68.125');order=await json('/api/sales-orders/'+order.id)
  expect(order.lines.map((l:any)=>l.line_amount)).toEqual(['50','18.125']);expect(order.state).toBe('DRAFT');expect(order.customer_id).toBe(fixtures.customers[0].id)
  await preserved();await page.screenshot({path:path.join(out,'initial-68-125.png'),fullPage:true});evidence.initial={lineAmounts:['50','18.125'],sum:'68.125',stockUnchanged:true,sourceIds:order.lines.map((l:any)=>l.id)}
})

test('B — exact edits/deletion, separate order SUM and header protection',async({page})=>{
  watch(page);page.on('dialog',d=>d.accept());await open(page)
  await page.getByRole('button',{name:'編輯明細 1',exact:true}).click();await page.getByLabel('單價 *',{exact:true}).fill('44');await page.getByRole('button',{name:'儲存明細',exact:true}).click();await expect(page.getByTestId('so-total')).toHaveText('73.125')
  await page.getByRole('button',{name:'編輯明細 1',exact:true}).click();await page.getByLabel('數量 *',{exact:true}).fill('2');await page.getByRole('button',{name:'儲存明細',exact:true}).click();await expect(page.getByTestId('so-total')).toHaveText('106.125')
  await page.getByRole('button',{name:'編輯明細 1',exact:true}).click();await page.getByLabel('數量 *',{exact:true}).fill('1.25');await page.getByRole('button',{name:'儲存明細',exact:true}).click();await expect(page.getByTestId('so-total')).toHaveText('73.125');order=await json('/api/sales-orders/'+order.id)
  other=await header(page,'SO-QA-DESKTOP-B-'+runId,1);await add(page,0,'3','0.1');await expect(page.getByTestId('so-total')).toHaveText('0.3')
  await page.getByRole('button',{name:'編輯明細 1',exact:true}).click();await page.getByLabel('單價 *',{exact:true}).fill('0.2');await page.getByRole('button',{name:'儲存明細',exact:true}).click();await expect(page.getByTestId('so-total')).toHaveText('0.6');expect((await json('/api/sales-orders/'+order.id)).total_amount).toBe('73.125')
  await page.getByRole('button',{name:'刪除明細 1',exact:true}).click();await expect(page.getByTestId('so-total')).toHaveText('0');expect((await json('/api/sales-orders/'+order.id)).total_amount).toBe('73.125')
  await page.getByRole('button',{name:'刪除銷售單',exact:true}).click();await expect(page.getByRole('heading',{name:'新增銷售單',exact:true})).toBeVisible()
  await open(page);await page.getByRole('button',{name:'刪除銷售單',exact:true}).click();await expect(page.getByRole('alert')).toContainText('仍有明細');await expect(page.getByRole('alert').locator('..').getByRole('button',{name:'刪除銷售單',exact:true})).toBeVisible()
  await page.getByRole('button',{name:'刪除明細 2',exact:true}).click();await expect(page.getByTestId('so-total')).toHaveText('55');order=await json('/api/sales-orders/'+order.id);expect(order.lines[0].line_amount).toBe('55');await preserved()
  evidence.editDelete={priceSum:'73.125',quantitySum:'106.125',secondOrderSums:['0.3','0.6','0'],firstAfterDeletion:'55',crossOrderIsolation:true,headerWithLinesProtected:true}
})

test('C — zero-mutation Cancel/Escape, search/F5 and true frontend/backend restart',async({page})=>{
  watch(page);await open(page);const saved=await json('/api/sales-orders/'+order.id)
  await cancelWithoutMutation(page,async()=>{await page.getByRole('button',{name:'編輯表頭',exact:true}).click();await page.getByLabel('備註',{exact:true}).fill('取消內容');await page.getByRole('button',{name:'取消',exact:true}).click()})
  await cancelWithoutMutation(page,async()=>{await page.getByRole('button',{name:'編輯明細 1',exact:true}).click();await page.getByLabel('單價 *',{exact:true}).fill('999');await page.getByRole('button',{name:'取消',exact:true}).click()})
  await cancelWithoutMutation(page,async()=>{await page.getByRole('button',{name:'＋ 新增明細',exact:true}).click();await page.getByLabel('數量 *',{exact:true}).fill('999');await page.keyboard.press('Escape')})
  expect(await json('/api/sales-orders/'+order.id)).toEqual(saved)
  await page.getByLabel('搜尋銷售單',{exact:true}).fill(fixtures.customers[0].code);await page.getByRole('button',{name:'搜尋',exact:true}).click();await expect(page.getByRole('button',{name:'開啟銷售單 '+order.document_no,exact:true})).toBeVisible();await page.reload();await expect(page.getByTestId('so-total')).toHaveText('55');expect(await json('/api/sales-orders/'+order.id)).toEqual(saved)
  await stop();await start();const newPids=listeners().map((s:any)=>s.OwningProcess);expect(newPids.every((n:number)=>!oldPids.includes(n))).toBe(true)
  await page.reload();await expect(page.getByTestId('so-total')).toHaveText('55');expect(await json('/api/sales-orders/'+order.id)).toEqual(saved);await preserved();expect(evidence.pageErrors).toEqual([])
  await page.screenshot({path:path.join(out,'after-restart-55.png'),fullPage:true});evidence.persistence={f5:true,restart:true,oldPids,newPids,sum:'55',fullRecordUnchanged:true};evidence.stock={finished:'1.25',raw:'3',unchanged:true};evidence.result='PASS'
})
