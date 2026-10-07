import { bootstrapAuth } from './auth'

// Establish identity through a synthetic /me response without adding a call to
// an API test's fetch mock. No production test-only authentication setter.
export async function authenticateTestWorkspace(
  requestContext = 'context-A', userId = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
): Promise<void> {
  const previous = globalThis.fetch
  globalThis.fetch = async (path) => {
    if (path !== '/api/auth/me') throw new Error('Only synthetic authentication bootstrap is permitted.')
    return new Response(JSON.stringify({ user_id: userId, request_context: requestContext }), { status: 200 })
  }
  try { await bootstrapAuth() } finally { globalThis.fetch = previous }
}
