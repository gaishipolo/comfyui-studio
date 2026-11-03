/** 登录页面 */
import { useState } from 'react'
import { login, setToken, setStoredUser } from '../api/client'

interface Props {
  onLogin: () => void
}

export default function Login({ onLogin }: Props) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setError('')
    setLoading(true)
    try {
      const resp = await login(username, password)
      setToken(resp.access_token)
      setStoredUser(resp.user)
      onLogin()
    } catch (err) {
      setError(err instanceof Error ? err.message : '登录失败')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="flex min-h-screen flex-col text-ink">
      <main className="flex flex-1 items-center justify-center px-md py-2xl">
        <div className="w-full max-w-form">
          <div className="mb-lg flex flex-col items-center text-center">
            <span
              aria-hidden="true"
              className="mb-md grid h-12 w-12 place-items-center rounded-lg bg-[linear-gradient(135deg,#6d6df0_0%,#5b5bd6_52%,#4646b8_100%)] text-accent-ink shadow-md"
            >
              <svg width="24" height="24" viewBox="0 0 24 24" fill="none" aria-hidden="true">
                <path
                  d="M12 3.5 20 8v8l-8 4.5L4 16V8l8-4.5Z"
                  stroke="currentColor"
                  strokeWidth="1.6"
                  strokeLinejoin="round"
                />
                <circle cx="12" cy="12" r="2.6" fill="currentColor" />
              </svg>
            </span>
            <h1 className="font-display text-xl font-semibold">ComfyUI 工作台</h1>
            <p className="mt-2xs text-sm text-muted">登录以继续使用</p>
          </div>

          <form
            onSubmit={handleSubmit}
            className="rounded-lg border border-rule bg-paper p-lg shadow-md"
          >
            <div>
              <label className="field-label" htmlFor="username">
                用户名
              </label>
              <input
                id="username"
                type="text"
                className="field"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                placeholder="请输入用户名"
                aria-label="请输入用户名"
                required
                autoFocus
              />
            </div>

            <div className="mt-md">
              <label className="field-label" htmlFor="password">
                密码
              </label>
              <input
                id="password"
                type="password"
                className="field"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder="请输入密码"
                aria-label="请输入密码"
                required
              />
            </div>

            {error && (
              <div
                role="alert"
                className="mt-md rounded-sm border border-error-line bg-error-soft px-sm py-xs text-sm text-error"
              >
                {error}
              </div>
            )}

            <button
              type="submit"
              disabled={loading}
              className="btn-primary mt-lg w-full justify-center"
            >
              {loading ? '登录中…' : '登 录'}
            </button>
          </form>
        </div>
      </main>
    </div>
  )
}
