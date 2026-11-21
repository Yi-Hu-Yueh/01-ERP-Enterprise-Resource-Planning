import { test, expect, type Page } from '@playwright/test'
import { execFile } from 'node:child_process'
import { promisify } from 'node:util'
import { mkdirSync, writeFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { tmpdir } from 'node:os'
import path from 'node:path'

const root = path.resolve(fileURLToPath(new URL('../../', import.meta.url)))
const artifacts = path.resolve(root, process.env.ERP_TEST_ARTIFACTS || 'artifacts/sqlite-maintenance-2026-11-21/desktop-attempt-01')
const edge = process.env.ERP_BROWSER_PATH || 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'
const runFile = promisify(execFile)
type Installation = { url: string; installation: string; database: string; seed: string; pid: number; [key: string]: unknown }
type MaintenanceStatus = {
  busy: boolean; operation: string | null;
  clear_eligible: boolean; clear_disabled_reason: string; restore_available: boolean;
  backups: { id: string; kind: string; integrity_status: string; business_row_count: number }[];
  database: { path: string; row_count: number; business_row_count: number };
  last_result: { operation: string; success: boolean; message: string } | null;
}
const statusSamples: { busy: boolean; operation: string | null; clear_eligible: boolean; clear_disabled_reason: string }[] = []
const modules = [
  { module: '基本資料', title: '原料維護' },
  { module: '採購管理', title: '採購訂單管理' },
  { module: '製造生產', title: '產品 BOM 用料表' },
  { module: '銷售管理', title: '銷售訂單管理' },
  { module: '庫存管理', title: '庫存盤點管理' },
  { module: '查詢與報表', title: '原料與成品即時庫存查詢' },
]
const normalized = (value: string) => value.replace(/\\/g, '/').toLowerCase()

test.use({
  viewport: { width: 1440, height: 1000 }, browserName: 'chromium',
  launchOptions: { executablePath: edge }, trace: 'off', screenshot: 'off',
})

function isolatedOrigin(installation: Installation) {
  const url = new URL(installation.url)
  expect(url.hostname).toBe('127.0.0.1')
  expect(url.port).toBeTruthy()
  expect(['5173', '8000']).not.toContain(url.port)
  const installationRoot = normalized(path.resolve(installation.installation))
  expect(installationRoot.startsWith(normalized(path.resolve(tmpdir())) + '/tiger-erp-sqlitemaintenance-')).toBe(true)
  expect(installationRoot.endsWith('/tiger-erp')).toBe(true)
  expect(normalized(path.resolve(installation.database))).toBe(normalized(path.resolve(installation.installation, 'data', 'erp.sqlite3')))
  expect(normalized(installation.database)).not.toBe(normalized(installation.seed))
  expect(normalized(installation.database)).not.toBe(normalized(path.join(root, 'data', 'erp.sqlite')))
  return url.origin
}

async function runner(...args: string[]): Promise<Installation> {
  const result = await runFile(path.join(root, '.venv', 'Scripts', 'python.exe'), [
    path.join(root, 'scripts', 'verify_sqlite_maintenance.py'), ...args,
  ], { cwd: root, windowsHide: true, timeout: 180_000, maxBuffer: 8 * 1024 * 1024 })
  if (result.stderr.trim()) writeFileSync(path.join(artifacts, 'runner-' + args[0] + '-stderr.txt'), result.stderr)
  return JSON.parse(result.stdout.trim()) as Installation
}

async function status(page: Page, installation: Installation) {
  let result: MaintenanceStatus | undefined
  // A concurrent status scan deliberately returns a fail-closed busy response.
  // Inspect authoritative eligibility only after the backend itself reports idle.
  await expect.poll(async () => {
    const response = await page.request.get(isolatedOrigin(installation) + '/api/sqlite-maintenance/status')
    expect(response.status()).toBe(200)
    result = await response.json() as MaintenanceStatus
    expect(normalized(result.database.path)).toBe(normalized(installation.database))
    statusSamples.push({ busy: result.busy, operation: result.operation, clear_eligible: result.clear_eligible, clear_disabled_reason: result.clear_disabled_reason })
    return result.busy
  }, { timeout: 15_000 }).toBe(false)
  return result!
}

async function moduleSweep(page: Page) {
  const found: string[] = []
  for (const item of modules) {
    await page.getByRole('navigation', { name: '主要模組' }).locator('button.nav-item').filter({ hasText: item.module }).click()
    await expect(page.getByRole('heading', { name: item.title, exact: true })).toBeVisible()
    const topbar = page.locator('header.topbar')
    const group = topbar.getByRole('group', { name: 'SQLite 資料維護' })
    await expect(group.getByRole('button')).toHaveText(['備份 SQLite', '清空 SQLite', '恢復 SQLite'])
    await expect(topbar.locator('.connection.connected')).toBeVisible()
    await expect(topbar.locator('.topbar-context > span')).toContainText('企業資源規劃')
    expect(await topbar.locator('.topbar-context').evaluate(element => {
      const children = Array.from(element.children)
      return children[0].tagName === 'SPAN' && children[1].classList.contains('sqlite-maintenance')
        && children[2].textContent?.startsWith('匯出獨立系統')
    })).toBe(true)
    const boxes = await group.getByRole('button').evaluateAll(buttons => buttons.map(button => {
      const rect = button.getBoundingClientRect(); return { x: rect.x, y: rect.y, width: rect.width }
    }))
    expect(boxes[0].y).toBe(boxes[1].y); expect(boxes[1].y).toBe(boxes[2].y)
    expect(boxes[0].x + boxes[0].width).toBeLessThan(boxes[1].x)
    expect(boxes[1].x + boxes[1].width).toBeLessThan(boxes[2].x)
    found.push(item.module)
  }
  return found
}

async function backup(page: Page, installation: Installation) {
  const response = page.waitForResponse(value => new URL(value.url()).pathname === '/api/sqlite-maintenance/backups' && value.request().method() === 'POST')
  await page.locator('header.topbar').getByRole('button', { name: '備份 SQLite', exact: true }).click()
  expect([200, 201]).toContain((await response).status())
  await expect(page.locator('header.topbar').getByRole('button', { name: '清空 SQLite', exact: true })).toBeEnabled()
  const result = await status(page, installation)
  expect(result.clear_eligible).toBe(true)
  expect(result.backups[0].integrity_status).toBe('VERIFIED')
  return result.backups.find(item => item.kind === 'backup')!
}

async function createWorkCenter(page: Page, installation: Installation, suffix: string) {
  const code = 'SQLITE-QA-' + suffix + '-' + Date.now()
  await page.goto(installation.url + '/#work-centers')
  await expect(page.getByRole('heading', { name: '工作中心維護', exact: true })).toBeVisible()
  await page.locator('#master-code').fill(code)
  await page.locator('#master-name').fill('隔離 SQLite 維護驗證 ' + suffix)
  await page.locator('#master-description').fill('只在本次隔離 EXE 的資料庫建立。')
  const response = page.waitForResponse(value => new URL(value.url()).pathname === '/api/work-centers' && value.request().method() === 'POST')
  await page.getByRole('button', { name: '儲存工作中心', exact: true }).click()
  expect((await response).status()).toBe(201)
  await expect(page.locator('.editor-panel').getByRole('status')).toContainText('已新增')
  return code
}

test('desktop isolated EXE SQLite backup, cancellation, atomic clear, exact restore and restart persistence', async ({ page }, testInfo) => {
  test.setTimeout(600_000)
  const zip = process.env.ERP_SQLITE_MAINTENANCE_ZIP
  if (!zip) throw new Error('ERP_SQLITE_MAINTENANCE_ZIP must identify the new QA standalone ZIP. This workflow never starts or mutates an Owner server.')
  mkdirSync(artifacts, { recursive: true })
  statusSamples.length = 0
  const startedAt = Date.now()
  const evidence: Record<string, unknown> = { browser: edge, viewport: 'desktop 1440x1000', realBackend: true, ownerDestructiveTests: false }
  const writes: { method: string; path: string }[] = []
  const pageErrors: string[] = []
  const blockedRequests: string[] = []
  let installation: Installation | undefined
  let allowedOrigin = ''
  page.on('pageerror', error => pageErrors.push(error.message))
  await page.route('**/api/**', async route => {
    const request = route.request(), url = new URL(request.url())
    if (url.origin !== allowedOrigin || ['5173', '8000'].includes(url.port)) {
      blockedRequests.push(request.method() + ' ' + request.url()); await route.abort('blockedbyclient'); return
    }
    if (['POST', 'PUT', 'PATCH', 'DELETE'].includes(request.method())) writes.push({ method: request.method(), path: url.pathname })
    await route.continue()
  })
  const count = (operation: string) => writes.filter(value => value.path === '/api/sqlite-maintenance/' + operation).length
  try {
    installation = await runner('start', zip)
    allowedOrigin = isolatedOrigin(installation)
    evidence.firstLaunch = installation
    await page.goto(installation.url + '/#raw-materials')
    const initial = await status(page, installation)
    evidence.initial = { rows: initial.database.row_count, businessRows: initial.database.business_row_count, backups: initial.backups.length }
    expect(initial.clear_eligible).toBe(false); expect(initial.restore_available).toBe(false)
    await expect(page.getByRole('button', { name: '清空 SQLite', exact: true })).toBeDisabled()
    await expect(page.getByRole('button', { name: '恢復 SQLite', exact: true })).toBeDisabled()
    evidence.modules = await moduleSweep(page)
    await page.goto(installation.url + '/#raw-materials')
    await page.locator('#name').fill('尚未儲存的草稿應被清空後移除')
    const savedBackup = await backup(page, installation)
    evidence.initialBackup = savedBackup

    await page.getByRole('button', { name: '清空 SQLite', exact: true }).click()
    const clearDialog = page.getByRole('dialog', { name: '清空 SQLite', exact: true })
    await expect(clearDialog).toBeVisible()
    await expect(clearDialog.getByLabel('已驗證備份資訊')).toBeVisible()
    await expect(clearDialog).toContainText(installation.database)
    await expect(clearDialog.getByRole('button', { name: '確認清空', exact: true })).toBeDisabled()
    await page.screenshot({ path: path.join(artifacts, 'clear-confirmation.png'), fullPage: false })
    await clearDialog.getByRole('button', { name: '取消', exact: true }).click()
    await expect(clearDialog).not.toBeVisible()
    expect(count('clear')).toBe(0)
    evidence.clearCancelUnchanged = await runner('check-unchanged')

    await page.getByRole('button', { name: '清空 SQLite', exact: true }).click()
    await clearDialog.locator('#sqlite-maintenance-confirmation').fill('清空')
    await expect(clearDialog.getByRole('button', { name: '確認清空', exact: true })).toBeEnabled()
    const clearResponse = page.waitForResponse(value => new URL(value.url()).pathname === '/api/sqlite-maintenance/clear' && value.request().method() === 'POST')
    await clearDialog.getByRole('button', { name: '確認清空', exact: true }).click({ clickCount: 2 })
    const clearReply = await clearResponse
    expect(clearReply.status()).toBe(200)
    const clearOutcome = await clearReply.json()
    expect(clearOutcome.committed).toBe(true)
    expect(clearOutcome.status.last_result.committed).toBe(true)
    evidence.clearOutcome = { committed: clearOutcome.committed, warning: !!clearOutcome.warning }
    await expect(clearDialog).not.toBeVisible()
    expect(count('clear')).toBe(1)
    await expect(page.locator('#name')).toHaveValue('')
    await expect(page.getByRole('button', { name: '清空 SQLite', exact: true })).toBeDisabled()
    expect((await status(page, installation)).database.business_row_count).toBe(0)
    evidence.emptyState = await runner('check-empty')
    await page.reload({ waitUntil: 'domcontentloaded' })
    expect(await page.evaluate(() => performance.getEntriesByType('navigation').some(entry => (entry as PerformanceNavigationTiming).type === 'reload'))).toBe(true)
    await expect(page.getByRole('button', { name: '恢復 SQLite', exact: true })).toBeEnabled()
    await expect(page.getByRole('button', { name: '清空 SQLite', exact: true })).toBeDisabled()
    expect((await status(page, installation)).backups.some(item => item.id === savedBackup.id)).toBe(true)

    const postClearCode = await createWorkCenter(page, installation, 'POSTCLEAR')
    evidence.emptyDatabaseUsable = postClearCode
    const workCentersBeforeCancel = await (await page.request.get(allowedOrigin + '/api/work-centers')).json()
    const beforeCancel = await status(page, installation)
    evidence.restoreCancelCheckpoint = await runner('checkpoint', 'restore-cancel')
    await page.getByRole('button', { name: '恢復 SQLite', exact: true }).click()
    const restoreDialog = page.getByRole('dialog', { name: '恢復 SQLite', exact: true })
    await expect(restoreDialog.locator('#sqlite-maintenance-backup')).toHaveValue(savedBackup.id)
    await expect(restoreDialog).toContainText('該備份之後新增或修改的紀錄可能被取代')
    await expect(restoreDialog.getByRole('button', { name: '確認恢復', exact: true })).toBeEnabled()
    await page.screenshot({ path: path.join(artifacts, 'restore-confirmation.png'), fullPage: false })
    await restoreDialog.getByRole('button', { name: '取消', exact: true }).click()
    await expect(restoreDialog).not.toBeVisible()
    expect(count('restore')).toBe(0)
    expect(await (await page.request.get(allowedOrigin + '/api/work-centers')).json()).toEqual(workCentersBeforeCancel)
    expect((await status(page, installation)).database).toEqual(beforeCancel.database)
    evidence.restoreCancelEntireDatabase = await runner('check-checkpoint', 'restore-cancel')
    evidence.restoreCancelNoMutation = true

    await page.getByRole('button', { name: '恢復 SQLite', exact: true }).click()
    await expect(restoreDialog.getByRole('button', { name: '確認恢復', exact: true })).toBeEnabled()
    const restoreResponse = page.waitForResponse(value => new URL(value.url()).pathname === '/api/sqlite-maintenance/restore' && value.request().method() === 'POST')
    await restoreDialog.getByRole('button', { name: '確認恢復', exact: true }).click({ clickCount: 2 })
    const restoreReply = await restoreResponse
    expect(restoreReply.status()).toBe(200)
    const restoreOutcome = await restoreReply.json()
    expect(restoreOutcome.committed).toBe(true)
    expect(restoreOutcome.status.last_result.committed).toBe(true)
    evidence.restoreOutcome = { committed: restoreOutcome.committed, warning: !!restoreOutcome.warning }
    await expect(restoreDialog).not.toBeVisible()
    expect(count('restore')).toBe(1)
    await expect(page.locator('#master-code')).toHaveValue('')
    await expect(page.getByRole('button', { name: '清空 SQLite', exact: true })).toBeDisabled()
    evidence.exactRestore = await runner('check-restored')
    const restored = await status(page, installation)
    expect(restored.database.row_count).toBe(initial.database.row_count)
    expect(restored.backups.some(item => item.kind === 'recovery')).toBe(true)
    expect(restored.last_result?.success).toBe(true)
    await page.screenshot({ path: path.join(artifacts, 'restored-state.png'), fullPage: false })

    await page.goto('about:blank')
    installation = await runner('restart')
    allowedOrigin = isolatedOrigin(installation)
    evidence.realExeRestart = installation
    await page.goto(installation.url + '/#work-centers')
    await expect(page.getByRole('heading', { name: '工作中心維護', exact: true })).toBeVisible()
    await expect(page.getByRole('button', { name: '恢復 SQLite', exact: true })).toBeEnabled()
    await expect(page.getByRole('button', { name: '清空 SQLite', exact: true })).toBeDisabled()
    const restarted = await status(page, installation)
    expect(restarted.database.row_count).toBe(initial.database.row_count)
    expect(restarted.backups.map(item => item.id)).toEqual(restored.backups.map(item => item.id))
    expect(restarted.last_result?.operation).toBe('restore')
    const fresh = await backup(page, installation)
    evidence.postRestartBackup = fresh
    const staleCode = await createWorkCenter(page, installation, 'STALE')
    await expect(page.getByRole('button', { name: '清空 SQLite', exact: true })).toBeDisabled()
    const stale = await status(page, installation)
    expect(stale.clear_eligible).toBe(false)
    expect(stale.clear_disabled_reason).toBeTruthy()
    await page.reload({ waitUntil: 'domcontentloaded' })
    await expect(page.getByRole('button', { name: '清空 SQLite', exact: true })).toBeDisabled()
    await expect(page.getByRole('button', { name: '恢復 SQLite', exact: true })).toBeEnabled()
    expect((await status(page, installation)).clear_eligible).toBe(false)
    evidence.staleAfterBusinessWriteAndF5 = { code: staleCode, reason: stale.clear_disabled_reason }
    expect(count('clear')).toBe(1); expect(count('restore')).toBe(1); expect(count('backups')).toBe(2)
    expect(blockedRequests).toEqual([]); expect(pageErrors).toEqual([])
    await page.screenshot({ path: path.join(artifacts, 'stale-backup-clear-disabled.png'), fullPage: false })
    evidence.result = 'PASS'
  } catch (error) {
    evidence.result = 'FAIL'; evidence.error = String(error)
    await page.screenshot({ path: path.join(artifacts, 'workflow-failure.png'), fullPage: false }).catch(() => {})
    throw error
  } finally {
    await page.goto('about:blank').catch(() => {})
    let finishFailure: unknown
    if (installation) {
      try { evidence.finish = await runner('finish') }
      catch (error) { finishFailure = error; evidence.finishError = String(error) }
    }
    const workflowPassed = evidence.result === 'PASS'
    if (finishFailure) evidence.result = 'FAIL'
    evidence.durationSeconds = (Date.now() - startedAt) / 1000
    evidence.writes = writes; evidence.pageErrors = pageErrors; evidence.blockedRequests = blockedRequests; evidence.statusSamples = statusSamples
    const file = path.join(artifacts, 'desktop-maintenance-evidence.json')
    writeFileSync(file, JSON.stringify(evidence, null, 2) + '\n')
    await testInfo.attach('desktop-maintenance-evidence', { path: file, contentType: 'application/json' })
    if (finishFailure && workflowPassed) throw finishFailure
  }
})
