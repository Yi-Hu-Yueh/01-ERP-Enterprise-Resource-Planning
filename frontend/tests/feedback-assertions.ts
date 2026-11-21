import { expect, type Page } from '@playwright/test'

export async function expectActionFeedback(page:Page,selector='.editor-panel .form-actions'){
  const group=page.locator(selector).filter({has:page.getByRole('alert')})
  const alert=group.getByRole('alert')
  await expect(alert).toBeVisible()
  const button=group.locator(':scope > button').first()
  await expect.poll(async()=>{
    const a=await alert.boundingBox(),b=await button.boundingBox(),viewport=page.viewportSize()
    if(!a||!b||!viewport)return false
    const gap=Math.max(b.y-a.y-a.height,a.y-b.y-b.height,0)
    return gap<=20&&a.y>=0&&a.y+a.height<=viewport.height&&b.y>=0&&b.y+b.height<=viewport.height
  }).toBe(true)
  await expect(alert).toBeFocused()
}
