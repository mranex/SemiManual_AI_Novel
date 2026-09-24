import { defineConfig } from '@playwright/test'

export default defineConfig({
  testDir: './e2e',
  timeout: 120_000,
  workers: 1,
  use: {
    baseURL: 'http://127.0.0.1:8001',
    browserName: 'chromium',
    launchOptions: { executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe' },
    headless: true,
  },
  webServer: {
    command: 'python ../tests/web/e2e_server.py',
    url: 'http://127.0.0.1:8001/api/v1/projects',
    reuseExistingServer: false,
    timeout: 30_000,
  },
})
