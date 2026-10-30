/** 修改密码弹窗 — 所有用户可改自己的密码
 *
 *  起因：admin 初始密码是后端启动时打到控制台的随机串，而此前**没有任何改密入口**，
 *  密码等于永远改不掉，只能翻日志或去改数据库。有了这个入口才算把洞补上。
 *
 *  后端会再校验一遍（旧密码不对 → 400；新旧相同 → 400；少于 6 位 → 422），
 *  这里做的是**提前**拦住，好让用户在按下按钮前就知道哪里填错了。 */
import { useState } from 'react'
import { changePassword } from '../api/client'

interface Props {
  onClose: () => void
  /** 改密成功后回调（顶栏可以提示一句，不必重登 —— token 不受影响） */
  onSuccess?: () => void
}

export default function ChangePasswordModal({ onClose, onSuccess }: Props) {
  const [oldPassword, setOldPassword] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [error, setError] = useState('')
  const [saving, setSaving] = useState(false)

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (saving) return

    // 本地先拦一道，省一次往返、也少一次「改错密码」的心惊
    if (newPassword.length < 6) {
      setError('新密码至少 6 位')
      return
    }
    if (newPassword !== confirm) {
      setError('两次输入的新密码不一致')
      return
    }
    if (newPassword === oldPassword) {
      setError('新密码不能与当前密码相同')
      return
    }

    setSaving(true)
    setError('')
    try {
      await changePassword(oldPassword, newPassword)
      onSuccess?.()
      onClose()
    } catch (err) {
      // 后端文案（「当前密码不正确」等）直接给用户看，别自己猜
      setError(err instanceof Error ? err.message : String(err))
      setSaving(false)
    }
  }

  return (
    <div
      className="fixed inset-0 z-modal flex items-center justify-center bg-scrim p-md"
      onClick={onClose}
    >
      <div
        className="flex w-full max-w-md flex-col border border-rule bg-paper"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between gap-xs border-b border-rule p-md">
          <h3 className="font-display text-title">修改密码</h3>
          <button type="button" onClick={onClose} className="x-btn" aria-label="关闭修改密码">
            ✕
          </button>
        </div>

        <form onSubmit={handleSubmit} className="p-md">
          <label className="field-label" htmlFor="old-password">
            当前密码
          </label>
          <input
            id="old-password"
            type="password"
            className="field"
            value={oldPassword}
            onChange={(e) => setOldPassword(e.target.value)}
            autoComplete="current-password"
            autoFocus
            required
          />

          <label className="field-label mt-md" htmlFor="new-password">
            新密码（至少 6 位）
          </label>
          <input
            id="new-password"
            type="password"
            className="field"
            value={newPassword}
            onChange={(e) => setNewPassword(e.target.value)}
            autoComplete="new-password"
            required
          />

          <label className="field-label mt-md" htmlFor="confirm-password">
            确认新密码
          </label>
          <input
            id="confirm-password"
            type="password"
            className="field"
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
            autoComplete="new-password"
            required
          />

          {error && (
            <p className="mt-md text-sm text-error" role="alert">
              {error}
            </p>
          )}

          <div className="mt-md flex justify-end gap-sm">
            <button type="button" onClick={onClose} className="btn-quiet">
              取消
            </button>
            <button type="submit" className="btn-primary" disabled={saving}>
              {saving ? '保存中…' : '保存'}
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}
