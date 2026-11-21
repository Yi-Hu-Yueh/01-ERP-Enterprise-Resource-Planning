import {test,expect,type APIRequestContext} from '@playwright/test'
import {spawn,execFileSync,type ChildProcess} from 'node:child_process'
import {mkdirSync,readFileSync,createWriteStream,writeFileSync} from 'node:fs'
import path from 'node:path'
import {fileURLToPath} from 'node:url'
import {randomUUID} from 'node:crypto'

const root=path.resolve(fileURLToPath(new URL('../../',import.meta.url)))
const task=path.join(root,'artifacts/round28-2026-11-21'),out=path.join(task,'desktop')
const py=path.join(root,'.venv/Scripts/python.exe'),db=path.join(out,'statistics-'+randomUUID()+'.sqlite')
const api='http://127.0.0.1:18448',ui='http://127.0.0.1:5211'
mkdirSync(out,{recursive:true})
let runtime:ChildProcess|undefined,metadata:any,before:any
const evidence:any={ownerDatabaseUsed:false,syntheticDatabase:db,workflows:1,mutations:[],pageErrors:[],responses:[]}
test.use({baseURL:ui,viewport:{width:1440,height:1100},isMobile:false})

function ps(script:string){return JSON.parse(execFileSync('powershell.exe',['-NoProfile','-Command',script],{windowsHide:true,encoding:'utf8'}))}
function listeners(){return ps('ConvertTo-Json -InputObject @(Get-NetTCPConnection -State Listen | Where-Object LocalPort -in 18448,5211 | Select-Object LocalAddress,LocalPort,OwningProcess) -Compress')}
function snapshot(){return JSON.parse(execFileSync(py,['-c',`from pathlib import Path
from contextlib import closing
import sqlite3,json,hashlib,sys
with closing(sqlite3.connect(Path(sys.argv[1]).resolve().as_uri()+'?mode=ro',uri=True)) as c:
 c.execute('PRAGMA query_only=ON');c.execute('BEGIN')
 names=[r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
 quote=lambda n:'"'+n.replace('"','""')+'"'
 data=dict(rows={n:c.execute('SELECT rowid,* FROM '+quote(n)+' ORDER BY rowid').fetchall() for n in names},schema=c.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name').fetchall())
 print(json.dumps(dict(digest=hashlib.sha256(json.dumps(data,sort_keys=True).encode()).hexdigest(),tables=len(names),business_rows=sum(len(v) for k,v in data['rows'].items() if k!='alembic_version'))))`,db],{cwd:root,windowsHide:true,encoding:'utf8'}))}
async function ready(url:string){await expect.poll(async()=>{try{return(await fetch(url,{signal:AbortSignal.timeout(1000)})).status}catch{return 0}},{timeout:45000}).toBe(200)}
async function stop(){
 if(!runtime?.pid||runtime.exitCode!==null)return
 const id=runtime.pid
 const process=ps('Get-CimInstance Win32_Process -Filter "ProcessId='+id+'" | Select-Object ProcessId,ExecutablePath,CommandLine | ConvertTo-Json -Compress')
 expect(process.CommandLine).toContain('backend.local_demo');expect(process.CommandLine).toContain(db)
 expect(process.ExecutablePath.toLowerCase()).toBe(py.toLowerCase())
 execFileSync('taskkill.exe',['/PID',String(id),'/T','/F'],{windowsHide:true,stdio:'pipe'})
 await expect.poll(listeners).toEqual([]);runtime=undefined
}
async function report(request:APIRequestContext,area:string,filters:Record<string,string>={}){
 const response=await request.get(api+'/api/business-statistics?'+new URLSearchParams({area,...filters}))
 expect(response.status()).toBe(200);return response.json()
}

test.beforeAll(async()=>{
 metadata=JSON.parse(readFileSync(path.join(task,'backend-tests/source-metadata.json'),'utf8'))
 const source=path.resolve(metadata.database)
 expect(source.toLowerCase().startsWith(path.join(task,'backend-tests').toLowerCase()+path.sep)).toBe(true)
 execFileSync(py,['-c',`from pathlib import Path
from contextlib import closing
import sqlite3,sys
source,target=map(lambda p:Path(p).resolve(),sys.argv[1:]);assert not target.exists()
with closing(sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)) as original,closing(sqlite3.connect(target)) as dest:original.backup(dest)`,source,db],{cwd:root,windowsHide:true,stdio:'pipe'})
 before=snapshot();expect(listeners()).toEqual([])
 runtime=spawn(py,['-u','-m','backend.local_demo','--database',db,'--backend-port','18448','--frontend-port','5211'],{cwd:root,windowsHide:true,stdio:['ignore','pipe','pipe']})
 runtime.stdout?.pipe(createWriteStream(path.join(out,'runtime.stdout.log')));runtime.stderr?.pipe(createWriteStream(path.join(out,'runtime.stderr.log')))
 await ready(api+'/api/health');await ready(ui)
})
test.afterAll(async()=>{await stop();writeFileSync(path.join(out,'desktop-evidence.json'),JSON.stringify(evidence,null,2)+'\n')})

test('one desktop workflow: three exact statistics areas, source evidence, filters and genuine refresh',async({page,request})=>{
 const started=Date.now()
 page.on('pageerror',e=>evidence.pageErrors.push(e.message))
 page.on('response',response=>{if(response.url().includes('/api/business-statistics'))evidence.responses.push({status:response.status(),url:response.url()})})
 await page.route('**/api/**',async route=>{if(!['GET','HEAD'].includes(route.request().method())){evidence.mutations.push(route.request().method());await route.abort('blockedbyclient')}else await route.continue()})
 async function check(area:string,expected:any){
  await expect(page.getByTestId('statistics-tab-'+area)).toHaveAttribute('aria-pressed','true')
  for(const count of expected.summary.counts)await expect(page.getByTestId('statistics-metric-'+count.key).getByTestId('statistics-value')).toHaveText(String(count.count))
  for(const key of new Set<string>(expected.summary.quantities.flatMap((q:any)=>Object.keys(q).filter(k=>!['unit_id','unit_code','unit_name'].includes(k))))){
   const card=page.getByTestId('statistics-metric-'+key)
   if(await card.count())await expect(card.getByTestId('statistics-value')).toHaveText(expected.summary.quantities.filter((q:any)=>key in q).map((q:any)=>q[key]))
  }
  await expect(page.getByTestId('statistics-summary')).not.toContainText('undefined')
  if(expected.summary.amounts.length)await expect(page.getByTestId('statistics-order-amounts').getByTestId('statistics-amount-value')).toHaveText(expected.summary.amounts.map((a:any)=>a.amount))
 }
 const purchasing=await report(request,'purchasing')
 await page.goto('/#inventory-inquiry')
 await page.getByRole('button',{name:'採購、製造、銷售統計',exact:true}).click()
 await expect(page.getByRole('heading',{name:'採購、製造、銷售統計',exact:true})).toBeVisible()
 await check('purchasing',purchasing)
 await page.getByTestId('statistics-expand-receipts').click()
 await expect(page.getByTestId('statistics-details-receipts').getByTestId('statistics-source-row')).toHaveCount(purchasing.details.find((s:any)=>s.key==='receipts').rows.length)
 await page.getByTestId('statistics-details-receipts').getByTestId('statistics-source-row').first().locator('summary').click()
 await expect(page.getByTestId('statistics-details-receipts')).toContainText('過帳識別')
 await page.getByTestId('statistics-summary').scrollIntoViewIfNeeded();await page.screenshot({path:path.join(out,'purchasing-summary.png')})
 const filters={date_from:'2026-10-01',date_to:'2026-10-01',party_id:metadata.statistics_purchase_order.supplier_id,item_id:metadata.raws[0].id,document_no:'%_MULTI'}
 await page.getByLabel('查詢起日（UTC）',{exact:true}).fill(filters.date_from)
 await page.getByLabel('查詢迄日（UTC）',{exact:true}).fill(filters.date_to)
 await page.getByLabel('供應商',{exact:true}).selectOption(filters.party_id)
 await page.getByLabel('原料',{exact:true}).selectOption(filters.item_id)
 await page.getByLabel('單據號碼（包含文字）',{exact:true}).fill(filters.document_no)
 await page.getByTestId('statistics-query').click();await expect(page.getByTestId('statistics-query')).toBeEnabled()
 await check('purchasing',await report(request,'purchasing',filters))
 await expect(page.getByTestId('statistics-scope')).toContainText('不是所選期間期末')
 await page.getByTestId('statistics-clear').click();await expect(page.getByTestId('statistics-query')).toBeEnabled();await check('purchasing',purchasing)
 await page.getByLabel('單據號碼（包含文字）',{exact:true}).fill('NO-SUCH-STATISTICS-RECORD')
 await page.getByTestId('statistics-query').click();await expect(page.getByTestId('statistics-empty')).toBeVisible()
 await page.getByTestId('statistics-clear').click();await expect(page.getByTestId('statistics-query')).toBeEnabled()

 await page.getByTestId('statistics-tab-manufacturing').click();await expect(page.getByTestId('statistics-query')).toBeEnabled()
 await check('manufacturing',await report(request,'manufacturing'))
 await expect(page.getByTestId('statistics-indicator-confirmed_good_share').first()).toBeVisible()
 await page.getByTestId('statistics-expand-completions').click()
 await expect(page.getByTestId('statistics-details-completions')).toContainText('COMP-STATS-PAIR')
 await page.getByLabel('製造工單',{exact:true}).selectOption(metadata.statistics_other_wo.id)
 await page.getByTestId('statistics-query').click();await expect(page.getByTestId('statistics-query')).toBeEnabled()
 await check('manufacturing',await report(request,'manufacturing',{work_order_id:metadata.statistics_other_wo.id}))
 await expect(page.getByTestId('statistics-indicator-confirmed_good_share')).toContainText('0.5 / 0.625')
 await page.getByTestId('statistics-summary').scrollIntoViewIfNeeded();await page.screenshot({path:path.join(out,'manufacturing-independent-metrics.png')})
 await page.getByTestId('statistics-clear').click();await expect(page.getByTestId('statistics-query')).toBeEnabled()

 await page.getByTestId('statistics-tab-sales').click();await expect(page.getByTestId('statistics-query')).toBeEnabled()
 const sales=await report(request,'sales');await check('sales',sales)
 await expect(page.getByTestId('statistics-order-amounts')).toContainText('幣別未記錄')
 await page.getByTestId('statistics-expand-customer_returns').click()
 await expect(page.getByTestId('statistics-details-customer_returns')).toContainText('CRET-STATS-NO-RESTOCK')
 await expect(page.getByTestId('statistics-details-customer_returns')).toContainText('未退庫；無實體入庫')
 const selected={party_id:metadata.customers[1].id,item_id:metadata.statistics_other_product.id,document_no:'SO-STATS-OTHER'}
 await page.getByLabel('客戶',{exact:true}).selectOption(selected.party_id)
 await page.getByLabel('成品',{exact:true}).selectOption(selected.item_id)
 await page.getByLabel('單據號碼（包含文字）',{exact:true}).fill(selected.document_no)
 await page.getByTestId('statistics-query').click();await expect(page.getByTestId('statistics-query')).toBeEnabled()
 await check('sales',await report(request,'sales',selected))
 const firstDocument=await page.evaluate(()=>performance.timeOrigin)
 await page.getByTestId('statistics-refresh').click();await expect(page.getByTestId('statistics-query')).toBeEnabled()
 await page.reload();await expect(page.getByTestId('statistics-query')).toBeEnabled()
 expect(await page.evaluate(()=>performance.timeOrigin)).toBeGreaterThan(firstDocument)
 await expect(page.getByLabel('客戶',{exact:true})).toHaveValue(selected.party_id)
 await expect(page.getByLabel('成品',{exact:true})).toHaveValue(selected.item_id)
 await expect(page.getByLabel('單據號碼（包含文字）',{exact:true})).toHaveValue(selected.document_no)
 await check('sales',await report(request,'sales',selected))
 await page.getByTestId('statistics-summary').scrollIntoViewIfNeeded();await page.screenshot({path:path.join(out,'sales-per-order-values-after-reload.png')})
 await page.getByTestId('statistics-clear').click();await expect(page.getByTestId('statistics-query')).toBeEnabled();await check('sales',sales)
 // A small affected-navigation check, not a repetition of Operation29's suite.
 await page.getByRole('button',{name:'庫存異動與製造批號雙向追溯',exact:true}).click()
 await expect(page.getByTestId('inventory-movement-row').first()).toBeVisible()
 const trace=await request.get(api+'/api/lot-traceability?mode=options');expect(trace.status()).toBe(200)
 await page.getByRole('button',{name:'採購、製造、銷售統計',exact:true}).click();await expect(page.getByTestId('statistics-query')).toBeEnabled()
 await check('purchasing',purchasing)
 expect((await page.locator('main button').allTextContents()).some(t=>/儲存|刪除|新增|過帳確認/.test(t))).toBe(false)
 expect(evidence.mutations).toEqual([]);expect(evidence.pageErrors).toEqual([])
 expect(evidence.responses.every((r:any)=>r.status===200)).toBe(true)
 const after=snapshot();expect(after).toEqual(before)
 Object.assign(evidence,{result:'PASS',wall_seconds:(Date.now()-started)/1000,before,after,genuineDocumentReload:true,areas:['purchasing','manufacturing','sales'],readOnly:true})
})
