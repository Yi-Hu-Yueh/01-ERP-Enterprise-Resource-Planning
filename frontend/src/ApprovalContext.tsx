import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from 'react'
import { api } from './api'
import { ActionFeedback } from './ActionFeedback'
import './Approval.css'

export type Department={id:string;code:string;name:string;parent_department_id:string|null;manager_employee_id:string|null;is_active:boolean;notes:string|null;row_version:number}
export type Employee={id:string;code:string;name:string;department_id:string;title:string|null;manager_employee_id:string|null;is_active:boolean;notes:string|null;role_ids:string[];row_version:number}
export type ApprovalRole={id:string;code:string;name:string;is_active:boolean;notes?:string|null;row_version:number}
export type SourceType='PURCHASE_ORDER'|'WORK_ORDER'|'SALES_ORDER'
export const sourceNames:Record<SourceType,string>={PURCHASE_ORDER:'採購訂單',WORK_ORDER:'製造工單',SALES_ORDER:'銷售訂單'}
export type Assignee={kind:'employee'|'role'|'requester_manager';employee_id?:string;role_id?:string;role_code?:string;role_name?:string}
export type Stage={name:string;assignees:Assignee[]}
export type Definition={id:string;code:string;name:string;source_type:SourceType;version:number;row_version:number;is_active:boolean;is_demo?:boolean;stages:Stage[]}
type Config={csrf_token:string;blueprints:Definition[]}
type ApprovalState={departments:Department[];employees:Employee[];roles:ApprovalRole[];departmentId:string;employeeId:string;employee?:Employee;pendingCount:number;error:string;setDepartment:(id:string)=>void;setEmployee:(id:string)=>void;refresh:()=>Promise<void>;mutate:<T>(path:string,body:unknown,method?:string)=>Promise<T>;refreshPending:()=>Promise<void>}
const ApprovalContext=createContext<ApprovalState|null>(null)
const selectionKey='tiger-erp-demo-operator-v1'
function savedSelection(){try{return JSON.parse(localStorage.getItem(selectionKey)||'{}') as {departmentId?:string;employeeId?:string}}catch{return {}}}
export const requestKey=()=>crypto.randomUUID()

export function ApprovalProvider({children}:{children:ReactNode}){
  const saved=useRef(savedSelection())
  const [departments,setDepartments]=useState<Department[]>([]),[employees,setEmployees]=useState<Employee[]>([]),[roles,setRoles]=useState<ApprovalRole[]>([])
  const [departmentId,setDepartmentId]=useState(saved.current.departmentId||''),[employeeId,setEmployeeId]=useState(saved.current.employeeId||'')
  const [error,setError]=useState(''),[pendingCount,setPendingCount]=useState(0),[loaded,setLoaded]=useState(false)
  const token=useRef(''),pendingSequence=useRef(0),employeeRef=useRef(employeeId)
  employeeRef.current=employeeId
  const refresh=useCallback(async()=>{
    try{const [deps,staff,rs]=await Promise.all([api<Department[]>('/departments'),api<Employee[]>('/employees'),api<ApprovalRole[]>('/approval/roles')]);setDepartments(deps);setEmployees(staff);setRoles(rs);setLoaded(true);setError('')}
    catch(e){setError((e as Error).message);throw e}
  },[])
  const mutate=useCallback(async<T,>(path:string,body:unknown,method='POST'):Promise<T>=>{
    // This token protects the local browser boundary; it is never employee authentication.
    if(!token.current)token.current=(await api<Config>('/approval/config')).csrf_token
    return api<T>(path,{method,headers:{'X-ERP-Approval-Token':token.current},body:JSON.stringify(body)})
  },[])
  useEffect(()=>{void refresh().catch(()=>{})},[refresh])
  useEffect(()=>{
    if(!loaded)return
    const dep=departments.find(d=>d.id===departmentId&&d.is_active)
    const emp=employees.find(e=>e.id===employeeId&&e.is_active&&e.department_id===departmentId)
    if(!dep){setDepartmentId('');setEmployeeId('')}else if(!emp)setEmployeeId('')
  },[departments,employees,departmentId,employeeId,loaded])
  useEffect(()=>{try{localStorage.setItem(selectionKey,JSON.stringify({departmentId,employeeId}))}catch{/* UI convenience only. */}},[departmentId,employeeId])
  const refreshPending=useCallback(async()=>{
    if(employeeId!==employeeRef.current)return
    const sequence=++pendingSequence.current
    if(!employeeId){setPendingCount(0);return}
    try{const rows=await api<unknown[]>('/approval/requests?'+new URLSearchParams({employee_id:employeeId,view:'inbox'}));if(sequence===pendingSequence.current&&employeeId===employeeRef.current)setPendingCount(rows.length)}catch{if(sequence===pendingSequence.current&&employeeId===employeeRef.current)setPendingCount(0)}
  },[employeeId])
  useEffect(()=>{setPendingCount(0);void refreshPending();const changed=()=>{void refreshPending()};window.addEventListener('erp:business-mutation',changed);return()=>window.removeEventListener('erp:business-mutation',changed)},[refreshPending])
  function setDepartment(id:string){setDepartmentId(id);if(!employees.some(e=>e.id===employeeId&&e.department_id===id&&e.is_active))setEmployeeId('')}
  function setEmployee(id:string){setEmployeeId(employees.some(e=>e.id===id&&e.department_id===departmentId&&e.is_active)?id:'')}
  return <ApprovalContext.Provider value={{departments,employees,roles,departmentId,employeeId,employee:employees.find(e=>e.id===employeeId),pendingCount,error,setDepartment,setEmployee,refresh,mutate,refreshPending}}>{children}</ApprovalContext.Provider>
}
export function useApproval(){const state=useContext(ApprovalContext);if(!state)throw new Error('ApprovalProvider missing');return state}
export function ApprovalSelectors(){
  const s=useApproval()
  return <nav className="approval-identity" aria-label="模擬操作員選擇">
    <div className="approval-selectors"><label>目前部門<select data-testid="current-department" value={s.departmentId} onChange={e=>s.setDepartment(e.target.value)}><option value="">請選擇部門</option>{s.departments.filter(d=>d.is_active).map(d=><option key={d.id} value={d.id}>{d.code} · {d.name}</option>)}</select></label><label>目前員工<select data-testid="current-employee" value={s.employeeId} onChange={e=>s.setEmployee(e.target.value)} disabled={!s.departmentId}><option value="">請選擇員工</option>{s.employees.filter(e=>e.is_active&&e.department_id===s.departmentId).map(e=><option key={e.id} value={e.id}>{e.code} · {e.name}</option>)}</select></label><a href="#approval-inbox" className="approval-pending" aria-label={'待我簽核 '+s.pendingCount+' 筆'}>待我簽核 <strong data-testid="approval-pending-count">{s.pendingCount}</strong></a></div>
    <div className="approval-simulation"><span>目前模擬員工：{s.employee?`${s.departments.find(d=>d.id===s.employee?.department_id)?.name||''} / ${s.employee.name}`:'尚未選擇'}</span><strong>測試模式／未驗證身分</strong></div><div data-feedback-group><ActionFeedback error={s.error}/></div>
  </nav>
}
export function EmployeeOptions({activeOnly=false,exclude=''}:{activeOnly?:boolean;exclude?:string}){const {employees,departments}=useApproval();return <>{employees.filter(e=>e.id!==exclude&&(!activeOnly||e.is_active)).map(e=><option key={e.id} value={e.id}>{e.code} · {e.name} / {departments.find(d=>d.id===e.department_id)?.name||'—'}{e.is_active?'':'（停用）'}</option>)}</>}
export function ApprovalHeading({title,children}:{title:string;children?:ReactNode}){return <div className="page-heading"><div><div className="eyebrow">POST-30 / LOCAL APPROVAL DEMONSTRATION</div><h1>{title}</h1><p>本機示範簽核；員工選擇未經身分驗證，簽核不變更原單據過帳或庫存。</p></div>{children}</div>}
export function ReadonlyData({value}:{value:unknown}){if(value===null||value===undefined)return <span>—</span>;if(Array.isArray(value))return <ol className="approval-snapshot-list">{value.map((v,i)=><li key={i}><ReadonlyData value={v}/></li>)}</ol>;if(typeof value==='object')return <dl className="approval-data">{Object.entries(value as Record<string,unknown>).map(([k,v])=><div key={k}><dt>{k}</dt><dd><ReadonlyData value={v}/></dd></div>)}</dl>;return <span>{String(value)}</span>}
