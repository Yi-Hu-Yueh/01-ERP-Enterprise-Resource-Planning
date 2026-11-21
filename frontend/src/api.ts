export async function api<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch('/api'+path, {...options, headers:{'Content-Type':'application/json',...options?.headers}})
  if (!response.ok) {
    const data = await response.json().catch(()=>({message:'服務無法回應，請稍後再試。'}))
    throw new Error(data.message || '操作未完成，請重新讀取後再試。')
  }
  if (options?.method && !['GET','HEAD','OPTIONS'].includes(options.method.toUpperCase())) window.dispatchEvent(new Event('erp:business-mutation'))
  return response.status===204 ? undefined as T : response.json()
}
