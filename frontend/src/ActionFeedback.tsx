import { useEffect, useRef } from 'react'

type Props={error?:string;notice?:string;onReload?:()=>void;busy?:boolean}

export function ActionFeedback({error='',notice='',onReload,busy=false}:Props){
  const ref=useRef<HTMLDivElement>(null)
  useEffect(()=>{
    if(!(error||notice)||!ref.current)return
    if(error)ref.current.focus({preventScroll:true})
    const group=ref.current.closest('[data-feedback-group]')||ref.current
    group.scrollIntoView({block:'nearest',inline:'nearest'})
  },[error,notice])
  if(!(error||notice))return null
  return <div ref={ref} className={'action-feedback message '+(error?'error':'success')}
    role={error?'alert':'status'} tabIndex={error?-1:undefined} aria-atomic="true">
    {error||notice}{error&&onReload&&<button type="button" disabled={busy} onClick={onReload}>重新讀取</button>}
  </div>
}
