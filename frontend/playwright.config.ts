import { defineConfig } from '@playwright/test'

// Run against the same local Vite/FastAPI pair used for manual testing.
export default defineConfig({
  testDir: './e2e',
  testMatch: '**/*.pw.ts',
  workers: 1,
  use: {
    baseURL: 'http://localhost:5173',
    browserName: 'chromium',
    launchOptions: {
      executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH,
    },
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
})
