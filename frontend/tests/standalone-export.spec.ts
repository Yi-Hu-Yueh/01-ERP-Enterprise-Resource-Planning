import { test, expect, type Page, type Request } from '@playwright/test'
import { execFile } from 'node:child_process'
import { promisify } from 'node:util'
import { mkdirSync, readFileSync, statSync, writeFileSync } from 'node:fs'
import { createHash } from 'node:crypto'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const root = path.resolve(fileURLToPath(new URL('../../', import.meta.url)))
const artifacts = path.join(root, 'artifacts', 'standalone-export-2026-11-21', 'desktop')
const ownerURL = process.env.ERP_STANDALONE_TEST_URL
const edge = process.env.ERP_BROWSER_PATH || 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'
const runFile = promisify(execFile)
const warning = `此匯出將包含目前 SQLite 資料庫的
全部資料，包括客戶、供應商、採購、
製造、庫存及銷售紀錄。

匯出後的系統與目前系統各自獨立，
後續資料不會自動同步。

請妥善保管匯出的 ZIP 檔案。`

type Installation = { url: string; installation: string; database: string; seed: string; exe: string; pid: number; browser_opened: boolean }
const modules = [
  { module: '基本資料', title: '原料維護' },
  { module: '採購管理', title: '採購訂單管理' },
  { module: '製造生產', title: '產品 BOM 用料表' },
  { module: '銷售管理', title: '銷售訂單管理' },
  { module: '庫存管理', title: '庫存盤點管理' },
  { module: '查詢與報表', title: '原料與成品即時庫存查詢' },
]

test.use({
  baseURL: ownerURL,
  viewport: { width: 1440, height: 1000 },
  browserName: 'chromium',
  acceptDownloads: true,
  launchOptions: { executablePath: edge, downloadsPath: path.join(root, '.tmp', 'standalone-dl') },
  trace: 'off', screenshot: 'off',
})

async function runner(...args: string[]): Promise<Installation> {
  const result = await runFile(path.join(root, '.venv', 'Scripts', 'python.exe'), [
    path.join(root, 'scripts', 'verify_standalone_export.py'), ...args,
  ], { cwd: root, windowsHide: true, timeout: 150_000, maxBuffer: 8 * 1024 * 1024 })
  if (result.stderr.trim()) writeFileSync(path.join(artifacts, 'runner-' + args[0] + '-stderr.txt'), result.stderr)
  const installation = JSON.parse(result.stdout.trim()) as Installation
  installation.url = installation.url.replace(/\/$/, '')
  return installation
}

async function moduleSweep(page: Page) {
  const found: string[] = []
  for (const item of modules) {
    await page.getByRole('navigation', { name: '主要模組' }).locator('button.nav-item').filter({ hasText: item.module }).click()
    await expect(page.getByRole('heading', { name: item.title, exact: true })).toBeVisible()
    const topbar = page.locator('header.topbar')
    await expect(topbar.getByRole('button', { name: '匯出獨立系統', exact: true })).toBeVisible()
    await expect(topbar.locator('.topbar-context > span')).toContainText('企業資源規劃')
    expect(await topbar.locator('.topbar-context').evaluate(element => {
      const children = Array.from(element.children)
      return children[0].tagName === 'SPAN' && children[1].textContent?.startsWith('匯出獨立系統')
    })).toBe(true)
    await expect(page.getByRole('navigation', { name: '主要模組' }).getByRole('button', { name: '匯出獨立系統', exact: true })).toHaveCount(0)
    found.push(item.module)
  }
  return found
}

test('desktop genuine Owner export, independent EXE modules and isolated create/edit/refresh/restart persistence', async ({ page }, testInfo) => {
  test.setTimeout(600_000)
  if (!ownerURL) throw new Error('ERP_STANDALONE_TEST_URL must identify the prepared genuine Owner UI; this workflow never starts or seeds an Owner server.')
  mkdirSync(artifacts, { recursive: true })
  const startedAt = Date.now()
  const evidence: Record<string, unknown> = { browser: edge, ownerURL, fixtureDataUsed: false, viewport: 'desktop 1440x1000' }
  const ownerWrites: { method: string; path: string; body: string | null }[] = []
  const pageErrors: string[] = []
  const consoleMessages: { type: string; text: string }[] = []
  const requestFailures: { url: string; error: string | undefined }[] = []
  const ownerOrigin = new URL(ownerURL).origin
  const recordRequest = (request: Request) => {
    if (new URL(request.url()).origin === ownerOrigin && ['POST', 'PUT', 'PATCH', 'DELETE'].includes(request.method())) {
      ownerWrites.push({ method: request.method(), path: new URL(request.url()).pathname, body: request.postData() })
    }
  }
  page.on('request', recordRequest)
  page.on('pageerror', error => pageErrors.push(error.message))
  page.on('console', message => consoleMessages.push({ type: message.type(), text: message.text() }))
  page.on('requestfailed', request => requestFailures.push({ url: request.url(), error: request.failure()?.errorText }))
  let isolatedStarted = false
  try {
    await page.goto(ownerURL + '/#raw-materials')
    await expect(page.getByRole('heading', { name: '原料維護', exact: true })).toBeVisible()
    evidence.ownerModules = await moduleSweep(page)
    await page.locator('header.topbar').getByRole('button', { name: '匯出獨立系統', exact: true }).click()
    const dialog = page.getByRole('dialog', { name: '匯出獨立系統', exact: true })
    await expect(dialog).toBeVisible()
    expect(await dialog.locator('#standalone-export-warning').textContent()).toBe(warning)
    await expect(dialog.locator('#standalone-export-sensitive')).toContainText('憑證或其他敏感值')
    const cancel = dialog.getByRole('button', { name: '取消', exact: true })
    await expect(cancel).toBeEnabled()
    await dialog.getByRole('button', { name: '確認匯出', exact: true }).focus()
    await page.keyboard.press('Tab')
    await expect(cancel).toBeFocused()
    await page.screenshot({ path: path.join(artifacts, 'owner-export-confirmation.png'), fullPage: false })
    await cancel.click()
    await expect(dialog).not.toBeVisible()
    expect(ownerWrites).toEqual([])
    evidence.confirmationCancelWrites = ownerWrites.length

    await page.locator('header.topbar').getByRole('button', { name: '匯出獨立系統', exact: true }).click()
    const postResponse = page.waitForResponse(response => new URL(response.url()).pathname === '/api/standalone-export/jobs' && response.request().method() === 'POST')
    await dialog.getByRole('button', { name: '確認匯出', exact: true }).click({ clickCount: 2 })
    const created = await postResponse
    expect([200, 202]).toContain(created.status())
    const job = await created.json()
    expect(job.id).toBeTruthy()
    evidence.jobId = job.id
    await expect(dialog.getByRole('button', { name: '關閉', exact: true })).toBeVisible()
    await dialog.getByRole('button', { name: '關閉', exact: true }).click()
    await page.getByRole('navigation', { name: '主要模組' }).locator('button.nav-item').filter({ hasText: '製造生產' }).click()
    await expect(page.getByRole('heading', { name: '產品 BOM 用料表', exact: true })).toBeVisible()
    await page.locator('header.topbar').getByRole('button', { name: /匯出獨立系統/ }).click()
    await expect(dialog).toBeVisible()
    await expect(dialog.getByRole('button', { name: '確認匯出', exact: true })).toHaveCount(0)
    await expect(dialog.getByRole('button', { name: '下載 ZIP', exact: true })).toBeVisible({ timeout: 240_000 })
    await expect(dialog.locator('.standalone-export-progress')).toHaveClass(/completed/)
    evidence.jobPreservedAcrossModuleChange = true

    const downloadAttempts: { url: string; filename: string; failure: string | null }[] = []
    const downloadEvent = page.waitForEvent('download', { timeout: 120_000 })
    await dialog.getByRole('button', { name: '下載 ZIP', exact: true }).click()
    const download = await downloadEvent
    const downloadFailure = await download.failure()
    downloadAttempts.push({ url: download.url(), filename: download.suggestedFilename(), failure: downloadFailure })
    evidence.downloadAttempts = downloadAttempts
    expect(downloadFailure, 'The initial real UI ZIP download must complete.').toBeNull()
    expect(download.suggestedFilename()).toBe('TIGER-ERP-Standalone.zip')
    const zip = path.join(artifacts, 'TIGER-ERP-Standalone.zip')
    expect(await download.failure()).toBeNull()
    await download.saveAs(zip)
    expect(statSync(zip).size).toBeGreaterThan(0)
    evidence.zip = { path: zip, bytes: statSync(zip).size, sha256: createHash('sha256').update(readFileSync(zip)).digest('hex') }
    expect(ownerWrites).toEqual([{ method: 'POST', path: '/api/standalone-export/jobs', body: JSON.stringify({ confirmed: true }) }])
    evidence.ownerBrowserWrites = ownerWrites
    await page.screenshot({ path: path.join(artifacts, 'owner-export-completed.png'), fullPage: false })

    let standalone: Installation
    if (process.env.ERP_STANDALONE_REUSE_STARTED === '1') {
      const state = JSON.parse(readFileSync(path.join(root, 'artifacts', 'standalone-export-2026-11-21', 'standalone-state.json'), 'utf8'))
      const comparison = await runFile(path.join(root, '.venv', 'Scripts', 'python.exe'), ['-c',
        "import hashlib,json,sys,zipfile; a=zipfile.ZipFile(sys.argv[1]); b=zipfile.ZipFile(sys.argv[2]); assert sorted(a.namelist())==sorted(b.namelist()); assert all(hashlib.sha256(a.read(n)).digest()==hashlib.sha256(b.read(n)).digest() for n in a.namelist()); print(json.dumps({'all_members_identical':True,'members':len(a.namelist())}))",
        state.zip, zip,
      ], { cwd: root, windowsHide: true, timeout: 60_000 })
      evidence.reusedStartupZipComparison = JSON.parse(comparison.stdout)
      standalone = { ...state, url: state.url.replace(/\/$/, '') }
    } else standalone = await runner('start', zip)
    isolatedStarted = true
    expect(standalone.browser_opened).toBe(true)
    expect(new URL(standalone.url).hostname).toBe('127.0.0.1')
    expect(['5173', '8000']).not.toContain(new URL(standalone.url).port)
    evidence.standaloneFirstLaunch = standalone
    await page.goto(standalone.url + '/#raw-materials')
    evidence.standaloneModules = await moduleSweep(page)
    await page.locator('header.topbar').getByRole('button', { name: '匯出獨立系統', exact: true }).click()
    await expect(dialog.getByRole('button', { name: '確認匯出', exact: true })).toBeVisible()
    await dialog.getByRole('button', { name: '取消', exact: true }).click()

    await page.goto(standalone.url + '/#work-centers')
    await expect(page.getByRole('heading', { name: '工作中心維護', exact: true })).toBeVisible()
    const code = 'EXPORT-QA-' + Date.now()
    const editedName = '獨立系統隔離測試已修改'
    await page.locator('#master-code').fill(code)
    await page.locator('#master-name').fill('獨立系統隔離測試')
    await page.locator('#master-description').fill('只存在於本次解壓縮的獨立資料庫。')
    const createResponse = page.waitForResponse(response => new URL(response.url()).pathname === '/api/work-centers' && response.request().method() === 'POST')
    await page.getByRole('button', { name: '儲存工作中心', exact: true }).click()
    expect((await createResponse).status()).toBe(201)
    await expect(page.locator('.editor-panel').getByRole('status')).toContainText('已新增')
    await page.locator('#master-name').fill(editedName)
    await page.getByRole('button', { name: '儲存變更', exact: true }).click()
    await expect(page.locator('.editor-panel').getByRole('status')).toContainText('變更已儲存')
    await page.reload({ waitUntil: 'networkidle' })
    expect(await page.evaluate(() => performance.getEntriesByType('navigation').some(entry => (entry as PerformanceNavigationTiming).type === 'reload'))).toBe(true)
    await page.getByRole('button', { name: '開啟 ' + code, exact: true }).click()
    await expect(page.locator('#master-name')).toHaveValue(editedName)
    const recordsResponse = await page.request.get(standalone.url + '/api/work-centers?q=' + encodeURIComponent(code))
    expect(recordsResponse.status()).toBe(200)
    const records = await recordsResponse.json()
    const saved = records.find((record: { code: string }) => record.code === code)
    expect(saved.name).toBe(editedName)
    evidence.isolatedMutation = { code, id: saved.id, name: saved.name, rowVersion: saved.row_version, actualDocumentRefresh: true }
    await page.screenshot({ path: path.join(artifacts, 'standalone-isolated-record.png'), fullPage: false })

    const restarted = await runner('restart', saved.id, editedName)
    evidence.standaloneRestart = restarted
    await page.goto(restarted.url + '/#work-centers')
    await page.getByRole('button', { name: '開啟 ' + code, exact: true }).click()
    await expect(page.locator('#master-name')).toHaveValue(editedName)
    await expect(page.locator('#master-description')).toHaveValue('只存在於本次解壓縮的獨立資料庫。')
    evidence.browserPersistedAfterExeRestart = true
    expect(ownerWrites).toHaveLength(1)
    expect(pageErrors).toEqual([])
    evidence.pageErrors = pageErrors
    evidence.result = 'PASS'
  } catch (error) {
    evidence.result = 'FAIL'
    evidence.error = String(error)
    await page.screenshot({ path: path.join(artifacts, 'workflow-failure.png'), fullPage: false }).catch(() => {})
    throw error
  } finally {
    if (isolatedStarted) evidence.finish = await runner('finish')
    evidence.durationSeconds = (Date.now() - startedAt) / 1000
    evidence.ownerBrowserWrites = ownerWrites
    evidence.pageErrors = pageErrors
    evidence.consoleMessages = consoleMessages
    evidence.requestFailures = requestFailures
    writeFileSync(path.join(artifacts, 'desktop-export-evidence.json'), JSON.stringify(evidence, null, 2) + '\n')
    await testInfo.attach('desktop-export-evidence', { path: path.join(artifacts, 'desktop-export-evidence.json'), contentType: 'application/json' })
  }
})
