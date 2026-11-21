import { test, expect } from '@playwright/test'
import { execFileSync } from 'node:child_process'
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import path from 'node:path'

// This owns a new Playwright browser context, never an Owner browser or profile.
// Traces are disabled because request headers may carry local anti-CSRF tokens.
test.use({actionTimeout:20_000,trace:'off',screenshot:'only-on-failure',viewport:{width:1440,height:1100}})
test.setTimeout(480_000)
const root=path.resolve('..'),task=path.join(root,'artifacts/electronic-approval-2026-11-21')
const output=path.join(root,process.env.ERP_TEST_ARTIFACTS||'artifacts/electronic-approval-2026-11-21/desktop')
const nativeState=()=>JSON.parse(readFileSync(path.join(task,'native-state.json'),'utf8'))
const seed=JSON.parse(readFileSync(path.join(task,'native-ui-final.json'),'utf8'))
const evidence:{[key:string]:any}={workflow:'one representative native desktop workflow',browser:'isolated Playwright Edge context',pageErrors:[],httpErrors:[],checks:[],requestIds:{}}
mkdirSync(output,{recursive:true})

test('native desktop: organization, all four-stage workflows, co-sign, return, withdrawal, refresh and restart',async({page,request})=>{
  const started=Date.now();let base=nativeState().url.replace(/\/$/,'')
  expect(nativeState().synthetic_only).toBe(true);expect(seed.synthetic_only).toBe(true)
  page.on('pageerror',error=>evidence.pageErrors.push(error.message))
  page.on('response',response=>{if(response.status()>=400)evidence.httpErrors.push({status:response.status(),path:new URL(response.url()).pathname})})
  async function get(route:string){const response=await request.get(base+'/api'+route);expect(response.status()).toBe(200);return response.json()}
  async function navigate(hash:string){await page.goto(base+'/#'+hash);await expect(page.getByText('測試模式／未驗證身分',{exact:true})).toBeVisible()}
  let employees:any[]=[],departments:any[]=[]
  async function selectEmployee(id:string){const employee=employees.find(e=>e.id===id);expect(employee).toBeTruthy();await page.getByTestId('current-department').selectOption(employee.department_id);await page.getByTestId('current-employee').selectOption(id);await expect(page.getByTestId('current-employee')).toHaveValue(id)}
  async function selectedRequest(id:string,actor?:string){const employeeId=actor||await page.getByTestId('current-employee').inputValue();return get('/approval/requests/'+id+'?employee_id='+employeeId)}
  async function perform(id:string,name:string,task?:any,reason?:string){
    const panel=page.getByTestId('approval-detail')
    if(task){await selectEmployee(task.assigned_employee_id);await expect(page.getByTestId('approval-task-'+task.id).getByRole('button',{name,exact:true})).toBeVisible();await page.getByTestId('approval-task-'+task.id).getByRole('button',{name,exact:true}).click()}
    else await panel.getByRole('button',{name,exact:true}).click()
    const form=page.getByRole('form',{name:'簽核操作表單'})
    if(reason)await form.getByLabel('原因（必填）').fill(reason)
    await form.getByLabel('操作說明／留言').fill('Synthetic desktop evidence')
    await form.getByRole('button',{name:'確認'+name,exact:true}).click()
    await expect(form).toHaveCount(0)
    return selectedRequest(id)
  }
  async function submit(type:string,requester:string,evidenceKey=type){
    await selectEmployee(requester);await navigate('approval-submit')
    await page.getByLabel('單據類型').selectOption(type)
    await page.getByLabel('既存單據').selectOption(seed.sources[type].id)
    await expect(page.getByLabel('簽核流程版本')).not.toHaveValue('')
    await page.getByLabel('申請說明').fill('Synthetic four-stage desktop review '+type)
    const response=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/approval/requests'&&r.request().method()==='POST')
    await page.getByRole('button',{name:'送出簽核申請',exact:true}).click()
    const posted=await response;expect(posted.status()).toBe(200);const value=await posted.json();expect(value.stages).toHaveLength(4)
    await expect(page.getByTestId('approval-current-stage')).toHaveText('目前第 1 關')
    evidence.requestIds[evidenceKey]=value.id;return value
  }
  try{
    await navigate('approval-departments')
    await page.getByRole('button',{name:'建立示範部門與員工',exact:true}).click()
    await expect(page.getByRole('status').filter({hasText:'示範部門、員工、角色與三套四關流程已備妥'})).toBeVisible()
    departments=await get('/departments');employees=await get('/employees');const counts=[departments.length,employees.length]
    await page.getByRole('button',{name:'建立示範部門與員工',exact:true}).click()
    await expect(page.getByRole('button',{name:'建立示範部門與員工',exact:true})).toBeEnabled()
    expect([(await get('/departments')).length,(await get('/employees')).length]).toEqual(counts)
    console.log('Completed approval checkpoint '+(evidence.checks.length+1));evidence.checks.push('Explicit initializer is idempotent; all organization rows are synthetic')
    const manager=employees.find(e=>e.is_active);expect(manager).toBeTruthy()
    await page.getByRole('button',{name:'新增部門',exact:true}).click()
    const depEditor=page.getByRole('region',{name:'部門編輯'})
    // Native semantics expose this section by its explicit aria-label.
    await depEditor.getByLabel('代碼',{exact:true}).fill('UI-QA-DEPT')
    await depEditor.getByLabel('部門名稱',{exact:true}).fill('合成桌面驗證部門')
    await depEditor.getByLabel('上層部門').selectOption(departments[0].id)
    await depEditor.getByLabel('部門主管').selectOption(manager.id)
    await depEditor.getByRole('button',{name:'儲存部門',exact:true}).click()
    await expect(depEditor.getByRole('status')).toHaveText('資料已儲存。')
    const qaDepartment=(await get('/departments')).find((d:any)=>d.code==='UI-QA-DEPT');expect(qaDepartment.parent_department_id).toBe(departments[0].id)
    await depEditor.getByLabel('部門名稱',{exact:true}).fill('這個未儲存變更必須取消')
    await page.setViewportSize({width:390,height:844})
    const mobileCancel=depEditor.getByRole('button',{name:'取消',exact:true})
    await mobileCancel.scrollIntoViewIfNeeded();await expect(mobileCancel).toBeVisible()
    const cancelBounds=await mobileCancel.boundingBox();expect(cancelBounds).toBeTruthy()
    expect(cancelBounds!.x).toBeGreaterThanOrEqual(0);expect(cancelBounds!.y).toBeGreaterThanOrEqual(0)
    expect(cancelBounds!.x+cancelBounds!.width).toBeLessThanOrEqual(390);expect(cancelBounds!.y+cancelBounds!.height).toBeLessThanOrEqual(844)
    await mobileCancel.click();await page.setViewportSize({width:1440,height:1100})
    evidence.mobile_cancel={viewport:{width:390,height:844},visible:true,bounds:cancelBounds,unsaved_draft_discarded:true}
    expect((await get('/departments')).find((d:any)=>d.id===qaDepartment.id).name).toBe('合成桌面驗證部門')
    await navigate('approval-employees');await page.getByRole('button',{name:'新增員工',exact:true}).click()
    const empEditor=page.getByRole('region',{name:'員工編輯'})
    await empEditor.getByLabel('代碼',{exact:true}).fill('UI-QA-REQUESTER');await empEditor.getByLabel('員工姓名',{exact:true}).fill('合成桌面申請員工')
    await empEditor.getByLabel('主要部門').selectOption(qaDepartment.id);await empEditor.getByLabel('職稱',{exact:true}).fill('桌面驗證申請人')
    await empEditor.getByLabel('直屬主管').selectOption(manager.id);await empEditor.getByRole('button',{name:'儲存員工',exact:true}).click()
    await expect(empEditor.getByRole('status')).toHaveText('資料已儲存。');employees=await get('/employees');departments=await get('/departments')
    const requester=employees.find(e=>e.code==='UI-QA-REQUESTER').id
    await selectEmployee(requester);await page.getByTestId('current-department').selectOption(manager.department_id)
    await expect(page.getByTestId('current-employee')).toHaveValue('')
    for(const option of await page.getByTestId('current-employee').locator('option').evaluateAll(nodes=>nodes.map(n=>(n as HTMLOptionElement).value).filter(Boolean)))expect(employees.find(e=>e.id===option).department_id).toBe(manager.department_id)
    await selectEmployee(requester);await page.reload();await expect(page.getByTestId('current-employee')).toHaveValue(requester)
    console.log('Completed approval checkpoint '+(evidence.checks.length+1));evidence.checks.push('Department/employee create, hierarchy/manager, desktop editing and actual mobile390x844 cancel with in-viewport control and discarded draft; filtering, immediate clear and full document reload selection persistence')
    const definitions=await get('/approval/definitions?employee_id='+requester)
    for(const type of ['PURCHASE_ORDER','WORK_ORDER','SALES_ORDER']){const d=definitions.find((v:any)=>v.source_type===type);expect(d.stages).toHaveLength(4);expect(d.stages[2].assignees).toHaveLength(2)}
    let po=await submit('PURCHASE_ORDER',requester)
    for(const stage of [1,2]){const task=po.stages[stage-1].tasks.find((t:any)=>t.status==='PENDING');po=await perform(po.id,'同意',task);expect(po.current_stage).toBe(stage+1)}
    const stage3=po.stages[2].tasks.filter((t:any)=>t.status==='PENDING');expect(stage3).toHaveLength(2);expect(new Set(stage3.map((t:any)=>t.assigned_employee_id)).size).toBe(2)
    po=await perform(po.id,'同意',stage3[0]);expect(po.current_stage).toBe(3);expect(po.stages[3].tasks).toHaveLength(0)
    await expect(page.getByTestId('approval-current-stage')).toHaveText('目前第 3 關');await page.screenshot({path:path.join(output,'po-one-cosigner.png')})
    await selectEmployee(stage3[1].assigned_employee_id);await expect(page.getByTestId('approval-pending-count')).toHaveText('1')
    await navigate('approval-inbox');await expect(page.getByTestId('approval-request-row').filter({hasText:seed.sources.PURCHASE_ORDER.document_no})).toBeVisible()
    await page.getByRole('button',{name:'查看簽核 '+seed.sources.PURCHASE_ORDER.document_no,exact:true}).click()
    po=await perform(po.id,'同意',stage3[1]);expect(po.current_stage).toBe(4)
    po=await perform(po.id,'同意',po.stages[3].tasks[0]);expect(po.status).toBe('APPROVED');expect(po.events.filter((e:any)=>e.action==='approve')).toHaveLength(5)
    await expect(page.getByTestId('approval-status')).toHaveText('已同意');console.log('Completed approval checkpoint '+(evidence.checks.length+1));evidence.checks.push('PO stages 1→2→3; one co-signer cannot advance, switched employee inbox/count updates, both unlock stage 4 and complete')
    let wo=await submit('WORK_ORDER',requester)
    for(const stage of [1,2])wo=await perform(wo.id,'同意',wo.stages[stage-1].tasks[0])
    const beforeReturn=wo.events.length;wo=await perform(wo.id,'退回上一關',wo.stages[2].tasks[0],'合成測試退回第二關');expect(wo.current_stage).toBe(2);expect(wo.events.length).toBe(beforeReturn+1)
    wo=await perform(wo.id,'同意',wo.stages[1].tasks[0]);expect(wo.current_stage).toBe(3)
    for(const task of wo.stages[2].tasks.filter((t:any)=>t.status==='PENDING'))wo=await perform(wo.id,'同意',task)
    wo=await perform(wo.id,'同意',wo.stages[3].tasks[0]);expect(wo.status).toBe('APPROVED');expect(wo.events.some((e:any)=>e.reason==='合成測試退回第二關')).toBe(true)
    console.log('Completed approval checkpoint '+(evidence.checks.length+1));evidence.checks.push('WO four stages including return to stage 2, fresh cycle approvals and preserved earlier events')
    let so=await submit('SALES_ORDER',requester)
    so=await perform(so.id,'退回上一關',so.stages[0].tasks[0],'合成測試退回申請人');expect(so.status).toBe('RETURNED')
    await selectEmployee(requester);so=await perform(so.id,'重新送出新審查');expect(so.attempt).toBe(2)
    for(const stage of [1,2])so=await perform(so.id,'同意',so.stages[stage-1].tasks[0])
    for(const task of so.stages[2].tasks.filter((t:any)=>t.status==='PENDING'))so=await perform(so.id,'同意',task)
    so=await perform(so.id,'同意',so.stages[3].tasks[0]);expect(so.status).toBe('APPROVED')
    console.log('Completed approval checkpoint '+(evidence.checks.length+1));evidence.checks.push('SO four stages, Stage 1 return to requester and resubmission creates attempt 2 without losing history')
    const withdrawal=await submit('PURCHASE_ORDER',requester,'PURCHASE_ORDER_WITHDRAWN');await selectEmployee(requester);const withdrawn=await perform(withdrawal.id,'申請人撤回');expect(withdrawn.status).toBe('WITHDRAWN')
    await navigate('approval-mine');await expect(page.getByTestId('approval-request-row')).toHaveCount(4)
    await navigate('approval-history?request='+po.id);await expect(page.getByTestId('approval-events').locator('tbody tr')).toHaveCount(po.events.length)
    await page.reload();await expect(page.getByTestId('approval-status')).toHaveText('已同意');await expect(page.getByTestId('approval-events').locator('tbody tr')).toHaveCount(po.events.length)
    console.log('Completed approval checkpoint '+(evidence.checks.length+1));evidence.checks.push('Requester withdrawal; mine and cross-workflow history; genuine full document reload preserves approval/events')
    const beforeRestart={po:await selectedRequest(po.id,requester),wo:await selectedRequest(wo.id,requester),so:await selectedRequest(so.id,requester)}
    execFileSync(path.join(root,'.venv/Scripts/python.exe'),['-X','utf8',path.join(root,'scripts/verify_approval_workflow.py'),'restart'],{cwd:root,windowsHide:true,stdio:'pipe',timeout:120000})
    base=nativeState().url.replace(/\/$/,'');await navigate('approval-history');await selectEmployee(requester)
    for(const [key,before] of Object.entries(beforeRestart)){const after=await selectedRequest((before as any).id,requester);expect(after.events).toEqual((before as any).events);expect(after.status).toBe('APPROVED');evidence.requestIds[key]=(before as any).id}
    await navigate('approval-history?request='+po.id);await expect(page.getByTestId('approval-status')).toHaveText('已同意')
    console.log('Completed approval checkpoint '+(evidence.checks.length+1));evidence.checks.push('Real native EXE frontend/backend stop/restart retains exact event history and state for all three workflows')
    expect(evidence.pageErrors).toEqual([]);expect(evidence.httpErrors).toEqual([])
    evidence.result='PASS'
  }finally{evidence.wall_seconds=(Date.now()-started)/1000;writeFileSync(path.join(output,'desktop-evidence.json'),JSON.stringify(evidence,null,2)+'\n')}
})
