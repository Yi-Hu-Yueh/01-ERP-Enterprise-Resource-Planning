import { useApproval, type Definition } from './ApprovalContext'

/** The same candidate controls serve standalone and in-document submission.
 * Resolution and distinct-actor validation remain authoritative in the engine. */
export function ApprovalAssignees({definition,requesterId,assignments,onChange,disabled=false}:{definition:Definition;requesterId:string;assignments:Record<string,string>;onChange:(value:Record<string,string>)=>void;disabled?:boolean}){
  const s=useApproval(),requester=s.employees.find(e=>e.id===requesterId)
  return <fieldset disabled={disabled}><legend>此次申請的簽核人解析</legend><p>同關必要簽核者須全數同意；多位候選人時請明確指定，指派由伺服器再次驗證。</p>{definition.stages.map((stage,si)=><div className="approval-resolve-stage" key={si}><strong>第 {si+1} 關：{stage.name}</strong>{stage.assignees.map((a,ai)=>{
    const key=`${si+1}.${ai+1}`
    const candidates=s.employees.filter(e=>e.is_active&&(a.kind==='employee'?e.id===a.employee_id:a.kind==='role'?s.roles.some(r=>r.id===a.role_id&&r.is_active)&&e.role_ids.includes(a.role_id||''):e.id===s.departments.find(d=>d.id===requester?.department_id)?.manager_employee_id))
    const label=a.kind==='role'?s.roles.find(r=>r.id===a.role_id)?.name||'角色':a.kind==='requester_manager'?'申請部門主管':'指定員工'
    return <label key={key}><span>{label}</span><select name={"assignee:"+key} aria-label={`第${si+1}關簽核人${ai+1}`} required value={assignments[key]||(candidates.length===1?candidates[0].id:'')} onChange={e=>onChange({...assignments,[key]:e.target.value})}><option value="">{candidates.length?'請指定候選人':'沒有合資格員工，請先檢查設定'}</option>{candidates.map(e=><option key={e.id} value={e.id}>{e.code} · {e.name}</option>)}</select></label>
  })}</div>)}</fieldset>
}
