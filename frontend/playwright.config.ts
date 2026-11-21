import { defineConfig } from '@playwright/test'
import path from 'node:path'
const artifacts=path.resolve(process.cwd(), '..', process.env.ERP_TEST_ARTIFACTS||'artifacts/round3')
export default defineConfig({
  testDir: './tests',
  timeout: 180_000,
  expect: { timeout: 15_000 },
  workers: 1,
  retries: 0,
  reporter: [
    ['list'],
    ['json', { outputFile: path.join(artifacts,'playwright.json') }],
    ['html', { outputFolder: path.join(artifacts,'playwright-report'), open: 'never' }],
  ],
  outputDir: path.join(artifacts,'playwright-output'),
  use: {
    baseURL: 'http://127.0.0.1:5174',
    browserName: 'chromium',
    viewport: { width: 1440, height: 1100 },
    trace: 'on',
    screenshot: 'only-on-failure',
    launchOptions: process.env.ERP_BROWSER_PATH ? { executablePath: path.resolve(process.env.ERP_BROWSER_PATH) } : {},
  },
})

