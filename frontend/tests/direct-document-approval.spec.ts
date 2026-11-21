import {test,expect} from '@playwright/test'
import {readFileSync} from 'node:fs'
test.use({trace:'off',viewport:{width:1440,height:1100}})
test.setTimeout(150000)
const {ui,api}=JSON.parse(readFileSync('../artifacts/direct-document-approval-2026-11-21/qa-ports.json','utf8'))
let employees:any[],departments:any[],headers:Record<string,string>
test.beforeAll(async({request})=>{
 const cfg=await (await request.get(api+'/approval/config')).json()
 headers={'Origin':ui,'X-ERP-Approval-Token':cfg.csrf_token}
 expect((await request.post(api+'/approval/demo-init',{headers,data:{request_key:crypto.randomUUID()}})).status()).toBe(200)
 employees=await (await request.get(api+'/employees')).json();departments=await (await request.get(api+'/departments')).json()
 const finance=employees.find(e=>e.code==='DEMO-FINANCE-MANAGER')
 const extra=employees.some(e=>e.code==='DIRECT-UI-SECOND-FINANCE')?null:await request.post(api+'/employees',{headers,data:{code:'DIRECT-UI-SECOND-FINANCE',name:'合成第二財務簽核人',department_id:finance.department_id,role_ids:finance.role_ids}})
 if(extra)expect(extra.status()).toBe(201)
})
for(const spec of [
 {kind:'PURCHASE_ORDER',hash:'purchase-orders',route:'purchase-orders',prefix:'po',title:'採購訂單管理',template:'DEMO-PO-APPROVAL',create:'儲存表頭',open:'開啟採購單 ',edit:'編輯表頭',save:'儲存表頭',note:'po-note'},
 {kind:'WORK_ORDER',hash:'work-orders',route:'work-orders',prefix:'wo',title:'製造工單管理',template:'DEMO-WO-APPROVAL',create:'儲存工單',open:'開啟工單 ',edit:'編輯工單',save:'儲存變更',note:'wo-note'},
 {kind:'SALES_ORDER',hash:'sales-orders',route:'sales-orders',prefix:'so',title:'銷售訂單管理',template:'DEMO-SO-APPROVAL',create:'儲存變更',open:'開啟銷售單 ',edit:'編輯表頭',save:'儲存變更',note:'so-note'}
])test(spec.kind+' direct create, cancel, confirm, duplicate, refresh and existing edit',async({page,request})=>{
 page.setDefaultTimeout(20000);const errors:string[]=[];page.on('pageerror',e=>errors.push(e.message))
 const requester=employees.find(e=>e.code==='DEMO-APPLICANT')
 await page.goto(ui+'/#'+spec.hash)
 await expect(page.getByTestId('current-department').locator('option')).toHaveCount(departments.length+1)
 await page.getByTestId('current-department').selectOption(requester.department_id)
 await expect(page.getByTestId('current-department')).toHaveValue(requester.department_id)
 await expect(page.getByTestId('current-employee')).toBeEnabled()
 await page.getByTestId('current-employee').selectOption(requester.id)
 await expect(page.getByRole('button',{name:'送出簽核',exact:true})).toHaveCount(0)
 const number='DIRECT-'+spec.prefix.toUpperCase()+'-'+Date.now()
 if(spec.prefix==='po'){
  await page.locator('#po-document').fill(number);await page.locator('#po-date').fill('2026-11-21')
  const suppliers=await (await request.get(api+'/suppliers')).json();await page.locator('#po-supplier').selectOption(suppliers[0].id)
 }else if(spec.prefix==='so'){
  await page.locator('#so-number').fill(number);await page.locator('#so-date').fill('2026-11-21')
  const customers=await (await request.get(api+'/customers')).json();await page.locator('#so-customer').selectOption(customers[0].id)
 }else{
  await page.locator('#wo-number').fill(number);const products=await (await request.get(api+'/finished-goods')).json();await page.locator('#wo-product').selectOption(products[0].id)
  await page.locator('#wo-qty').fill('2.5');await page.locator('#wo-start').fill('2026-11-21');await page.locator('#wo-finish').fill('2026-11-25')
 }
 await page.getByRole('button',{name:spec.create,exact:true}).click()
 await expect(page.getByRole('button',{name:spec.edit,exact:true})).toBeVisible()
 const rows=await (await request.get(api+'/'+spec.route+'?q='+number)).json();const doc=rows.find((r:any)=>r.document_no===number);expect(doc).toBeTruthy()
 await page.getByRole('button',{name:spec.open+number,exact:true}).click()
 const send=page.getByRole('button',{name:'送出簽核',exact:true});await expect(send).toBeEnabled()
 async function records(){return (await request.get(api+'/approval/requests?'+new URLSearchParams({employee_id:requester.id,source_type:spec.kind,source_id:doc.id}))).json()}
 await send.click();const modal=page.getByRole('dialog',{name:'確認單據送簽'})
 await expect(modal.getByTestId('direct-document-number')).toHaveText(number)
 await expect(modal.getByTestId('direct-template')).toContainText(spec.template)
 await expect(modal.locator('select')).toHaveCount(5)
 for(let stage=1;stage<=4;stage++)await expect(modal.getByText('第 '+stage+' 關：',{exact:false})).toBeVisible()
 if(spec.prefix==='po')await expect(modal.getByLabel('第3關簽核人1',{exact:true})).toHaveValue('')
 await page.setViewportSize({width:390,height:844});const cancel=modal.getByRole('button',{name:'取消',exact:true});await cancel.scrollIntoViewIfNeeded();await expect(cancel).toBeVisible();await cancel.click()
 expect(await records()).toEqual([]);await page.setViewportSize({width:1440,height:1100})
 await send.click()
 if(spec.prefix==='po'){
  // Another writer changes the document after the confirmation preview.
  const current=await (await request.get(api+'/'+spec.route+'/'+doc.id)).json()
  const body=Object.fromEntries(['document_no','supplier_id','supplier_name_snapshot','currency_id','order_date','delivery_address_snapshot','payment_terms_snapshot','note','row_version'].map(k=>[k,current[k]]));body.note='Synthetic concurrent persisted edit'
  expect((await request.put(api+'/'+spec.route+'/'+doc.id,{data:body})).status()).toBe(200)
 }
 async function choose(){await expect(modal).toBeVisible();await expect(modal.locator('select')).toHaveCount(5);for(const field of await modal.locator('select').all())if(!await field.inputValue()){const values=await field.locator('option').evaluateAll(xs=>xs.map(x=>(x as HTMLOptionElement).value).filter(Boolean));await field.selectOption(values[0])}}
 await choose();await modal.getByLabel('申請說明',{exact:true}).fill('Direct synthetic UI submission')
 if(spec.prefix==='po'){
  await modal.getByRole('button',{name:'確認送簽',exact:true}).click();await expect(modal.getByRole('alert')).toContainText('來源單據已變更')
  expect(await records()).toEqual([]);await modal.getByRole('button',{name:'取消',exact:true}).click();await page.reload();await expect(send).toBeEnabled();await send.click();await choose()
 }
 const co=await modal.getByLabel('第3關簽核人1',{exact:true}).inputValue(),other=await modal.getByLabel('第3關簽核人2',{exact:true}).inputValue();expect(co).not.toBe(other)
 await modal.getByRole('button',{name:'確認送簽',exact:true}).click();await expect(modal).toHaveCount(0)
 await expect(page.getByRole('heading',{name:spec.title,exact:true})).toBeVisible()
 await expect(page.getByTestId('document-approval-status')).toContainText('簽核中')
 expect(await records()).toHaveLength(1);await expect(send).toBeDisabled()
 const posted=(await records())[0]
 const duplicate=await request.post(api+'/approval/requests',{headers,data:{employee_id:requester.id,request_key:crypto.randomUUID(),definition_id:posted.definition_id,source_type:spec.kind,source_id:doc.id,assignments:{'3.1':co}}})
 expect(duplicate.status()).toBe(409);expect(await records()).toHaveLength(1)
 await page.reload();await expect(page.getByTestId('document-approval-status')).toContainText('第 1 關')
 await expect(page.getByRole('link',{name:'查看簽核進度／歷程'})).toHaveAttribute('href','#approval-history?request='+posted.id)
 // Approval is not an edit/posting authorization gate.
 await page.getByRole('button',{name:spec.edit,exact:true}).click();await expect(send).toBeDisabled()
 await page.locator('#'+spec.note).fill('Saved after approval without unintended lock')
 await page.getByRole('button',{name:spec.save,exact:true}).click();await expect(page.getByTestId('document-approval-status')).toContainText('來源已變更')
 // Direct resubmission uses the existing request action/cycle, not a second request.
 await expect(send).toBeEnabled();await send.click();await choose();await modal.getByRole('button',{name:'確認送簽',exact:true}).click();await expect(modal).toHaveCount(0)
 const retry=(await records())[0];expect(retry.id).toBe(posted.id);expect(retry.attempt).toBe(2)
 expect(errors).toEqual([])
})
