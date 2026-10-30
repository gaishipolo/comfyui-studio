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
    <div className="flex min-h-screen flex-col bg-paper text-ink">
      <header className="rule-b">
        <div className="container py-sm">
          <h1 className="font-display text-display">ComfyUI 工作台</h1>
        </div>
      </header>

      <main className="container flex flex-1 items-start py-3xl">
        <div className="w-full max-w-form">
          <p className="text-lead text-ink-2">登录以继续使用</p>

          <form onSubmit={handleSubmit} className="mt-lg border border-rule bg-paper-2 p-lg">
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
              <div role="alert" className="mt-md border border-error bg-paper px-sm py-xs text-sm text-error">
                {error}
              </div>
            )}

            <button
              type="submit"
              disabled={loading}
              className="btn btn-primary mt-lg w-full justify-center"
            >
              {loading ? '登录中…' : '登 录'}
            </button>
          </form>
        </div>
      </main>
    </div>
  )
}
