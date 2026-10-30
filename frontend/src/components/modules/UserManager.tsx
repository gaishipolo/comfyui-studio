/** 用户管理（仅 admin）— 建号之后没人管得了账号的后续
 *
 *  背景：admin 初始密码是启动时打到控制台的随机串，此前既不能改自己的密码，
 *  也不能重置别人的；账号停用/角色调整更是没有入口。这个模块把账号的
 *  「改密之外」的日常管理补齐。
 *
 *  后端两条硬规则会以 400 + 可读文案拒绝，这里原样呈现，不做二次猜测：
 *    - 不能停用自己 / 不能取消自己的管理员身份
 *    - 系统必须保留至少一个启用的管理员
 *  为了少撞规则，对「自己」那两行直接把危险操作置灰，并说明原因。 */
import { useCallback, useEffect, useState } from 'react'
import type { User } from '../../types'
import { listUsers, registerUser, resetUserPassword, updateUser } from '../../api/client'
import type { ModuleProps } from '../../modules/registry'

export default function UserManager({ user }: ModuleProps) {
  const [users, setUsers] = useState<User[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  // 正在「改密」的那一行：user.id -> 填到一半的新密码。空串表示没有行展开
  const [resetFor, setResetFor] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [busyId, setBusyId] = useState('')

  // 新建用户表单（收起时 all 为空）
  const [showCreate, setShowCreate] = useState(false)
  const [draft, setDraft] = useState({ username: '', display_name: '', password: '', role: 'member' })

  const reload = useCallback(() => {
    setLoading(true)
    setError('')
    listUsers()
      .then(setUsers)
      .catch((err) => setError(err instanceof Error ? err.message : String(err)))
      .finally(() => setLoading(false))
  }, [])

  useEffect(reload, [reload])

  /** 账号类操作的统一收尾：失败文案直接显示后端返回的（规则说明在里面） */
  const run = async (id: string, action: () => Promise<unknown>, okMsg: string) => {
    setBusyId(id)
    setError('')
    setNotice('')
    try {
      await action()
      setNotice(okMsg)
      reload()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusyId('')
    }
  }

  const handleReset = async (u: User) => {
    const pwd = newPassword.trim()
    if (pwd.length < 6) {
      setError('新密码至少 6 位')
      return
    }
    await run(
      u.id,
      () => resetUserPassword(u.id, pwd),
      `已重置「${u.display_name || u.username}」的密码`,
    )
    setResetFor('')
    setNewPassword('')
  }

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault()
    const username = draft.username.trim()
    if (username.length < 2) {
      setError('用户名至少 2 个字符')
      return
    }
    if (draft.password.length < 6) {
      setError('初始密码至少 6 位')
      return
    }
    await run(
      username,
      () =>
        registerUser({
          username,
          password: draft.password,
          display_name: draft.display_name.trim() || undefined,
          role: draft.role as 'admin' | 'member',
        }),
      `已创建账号「${username}」`,
    )
    setShowCreate(false)
    setDraft({ username: '', display_name: '', password: '', role: 'member' })
  }

  return (
    <div>
      <div className="flex flex-wrap items-baseline justify-between gap-sm">
        <div>
          <h2 className="font-display text-headline">用户管理</h2>
          <p className="text-sm text-muted">
            建号、调整账号角色与启用状态、为忘记密码的成员重置密码。
          </p>
        </div>
        <div className="flex gap-sm">
          <button
            className={showCreate ? 'btn-quiet' : 'btn-primary'}
            onClick={() => {
              setShowCreate(!showCreate)
              setError('')
            }}
          >
            {showCreate ? '收起' : '新建用户'}
          </button>
          <button className="btn-quiet" onClick={reload} disabled={loading}>
            刷新
          </button>
        </div>
      </div>

      {/* 新建用户 — 账号从无到有只能靠 admin，别让用户去翻 register 接口 */}
      {showCreate && (
        <form
          onSubmit={handleCreate}
          className="mt-md flex flex-wrap items-end gap-sm border border-rule bg-paper-2 p-md"
        >
          <div>
            <label className="field-label" htmlFor="new-username">
              用户名
            </label>
            <input
              id="new-username"
              className="field"
              value={draft.username}
              onChange={(e) => setDraft({ ...draft, username: e.target.value })}
              required
            />
          </div>
          <div>
            <label className="field-label" htmlFor="new-display">
              显示名（可选）
            </label>
            <input
              id="new-display"
              className="field"
              value={draft.display_name}
              onChange={(e) => setDraft({ ...draft, display_name: e.target.value })}
            />
          </div>
          <div>
            <label className="field-label" htmlFor="new-pwd">
              初始密码（至少 6 位）
            </label>
            <input
              id="new-pwd"
              type="password"
              className="field"
              value={draft.password}
              onChange={(e) => setDraft({ ...draft, password: e.target.value })}
              required
            />
          </div>
          <div>
            <label className="field-label" htmlFor="new-role">
              角色
            </label>
            <select
              id="new-role"
              className="field"
              value={draft.role}
              onChange={(e) => setDraft({ ...draft, role: e.target.value })}
            >
              <option value="member">成员</option>
              <option value="admin">管理员</option>
            </select>
          </div>
          <button type="submit" className="btn-primary" disabled={busyId !== ''}>
            创建
          </button>
        </form>
      )}

      {/* 后端规则拒绝 / 操作成功 —— 都在表格上方给一句明确反馈 */}
      {error && (
        <p className="mt-md border border-rule bg-paper-2 p-sm text-sm text-error" role="alert">
          {error}
        </p>
      )}
      {notice && (
        <p className="mt-md border border-rule bg-paper-2 p-sm text-sm text-ink" role="status">
          {notice}
        </p>
      )}

      <div className="mt-md overflow-x-auto">
        {loading ? (
          <div className="space-y-sm" aria-hidden="true">
            {Array.from({ length: 4 }).map((_, i) => (
              <div key={i} className="skeleton h-md" />
            ))}
          </div>
        ) : users.length === 0 ? (
          <p className="py-lg text-center text-muted">还没有用户</p>
        ) : (
          <table className="table">
            <caption className="visually-hidden">系统用户</caption>
            <thead>
              <tr>
                <th scope="col">用户名</th>
                <th scope="col">显示名</th>
                <th scope="col">角色</th>
                <th scope="col">状态</th>
                <th scope="col">创建时间</th>
                <th scope="col">操作</th>
              </tr>
            </thead>
            <tbody>
              {users.map((u) => {
                const isSelf = u.id === user.id
                const busy = busyId === u.id
                return (
                  <tr key={u.id}>
                    <th scope="row" className="text-left">
                      {u.username}
                      {isSelf && <span className="chip ml-2xs">我</span>}
                    </th>
                    <td>{u.display_name || '—'}</td>
                    <td>
                      {/* 只有 admin 和 member 两种；对自己降级会被后端拒，索性置灰 */}
                      <select
                        className="field"
                        value={u.role}
                        disabled={busy || isSelf}
                        title={isSelf ? '不能取消自己的管理员身份' : undefined}
                        onChange={(e) =>
                          run(
                            u.id,
                            () => updateUser(u.id, { role: e.target.value as 'admin' | 'member' }),
                            `「${u.display_name || u.username}」的角色已更新`,
                          )
                        }
                      >
                        <option value="admin">管理员</option>
                        <option value="member">成员</option>
                      </select>
                    </td>
                    <td>
                      <span className="chip" data-tone={u.is_active ? 'success' : 'neutral'}>
                        {u.is_active ? '启用' : '停用'}
                      </span>
                    </td>
                    <td className="text-xs text-muted">
                      {u.created_at ? u.created_at.slice(0, 10) : '—'}
                    </td>
                    <td>
                      {resetFor === u.id ? (
                        <div className="flex flex-wrap items-center gap-2xs">
                          <input
                            type="password"
                            className="field"
                            placeholder="新密码（至少 6 位）"
                            value={newPassword}
                            onChange={(e) => setNewPassword(e.target.value)}
                            autoFocus
                          />
                          <button
                            className="btn-primary"
                            disabled={busy}
                            onClick={() => handleReset(u)}
                          >
                            {busy ? '…' : '确定'}
                          </button>
                          <button
                            className="btn-quiet"
                            onClick={() => {
                              setResetFor('')
                              setNewPassword('')
                            }}
                          >
                            取消
                          </button>
                        </div>
                      ) : (
                        <div className="flex flex-wrap gap-2xs">
                          <button
                            className="btn-quiet"
                            disabled={busy}
                            onClick={() => {
                              setResetFor(u.id)
                              setNewPassword('')
                            }}
                          >
                            重置密码
                          </button>
                          <button
                            className={u.is_active ? 'btn-danger-quiet' : 'btn-quiet'}
                            disabled={busy || (isSelf && u.is_active)}
                            title={isSelf && u.is_active ? '不能停用自己的账号' : undefined}
                            onClick={() =>
                              run(
                                u.id,
                                () => updateUser(u.id, { is_active: !u.is_active }),
                                `「${u.display_name || u.username}」已${u.is_active ? '停用' : '启用'}`,
                              )
                            }
                          >
                            {u.is_active ? '停用' : '启用'}
                          </button>
                        </div>
                      )}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        )}
      </div>
    </div>
  )
}
