import { expect, test } from 'vitest'
import config from '../vite.config.ts?raw'
import auth from './auth.ts?raw'
import interviewApi from './interviewApi.ts?raw'
import historyApi from './historyApi.ts?raw'

test('development proxies only same-origin API paths to the existing local backend', () => {
  expect(config).toContain("'/api': 'http://127.0.0.1:8000'")
  const proxy = /proxy:\s*\{([^}]+)\}/.exec(config)?.[1]
  expect(proxy?.match(/['"]\/[^'"]+['"]\s*:/g)).toEqual(["'/api':"])
  for (const source of [auth, interviewApi, historyApi]) {
    expect(source).not.toMatch(/https?:\/\/(?:localhost|127\.0\.0\.1)/)
  }
})
