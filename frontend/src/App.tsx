/**
 * 主应用 — 认证 + 按模块注册表动态渲染
 *
 * 所有功能 Tab 由 modules/ 注册表驱动，新增模块无需改本文件。
 */
import { useState, useEffect, useCallback, useMemo, useRef } from 'react'
import type { EngineInfo, GenerateDraft, Task, Template, User } from './types'
import {
  getTemplates, getHealth, getEngines, listTasks,
  getToken, getStoredUser, clearToken,
} from './api/client'
import { getVisibleModules } from './modules'
import {
  subscribe as subscribeToasts, dismiss as dismissToast,
  notifyOnce, emitAssetsChanged,
} from './notify'
import { friendlyError } from './components/FriendlyError'
import type { Toast } from './notify'
import Login from './components/Login'
import ChangePasswordModal from './components/ChangePasswordModal'

export default function App() {
  const [user, setUser] = useState<User | null>(getStoredUser())
  const [templates, setTemplates] = useState<Template[]>([])
  const [health, setHealth] = useState<{ status: string; comfyui: string } | null>(null)
  /** 各引擎的健康状态。多引擎之后光看 ComfyUI 会骗人 ——
   *  用户选的可能是 grsai，它挂了顶部还显示「在线」，然后任务莫名失败 */
  const [engines, setEngines] = useState<EngineInfo[] | null>(null)
  const [activeKey, setActiveKey] = useState('generate')
  const [showChangePassword, setShowChangePassword] = useState(false)
  const [notice, setNotice] = useState('')
  /** 「用这些参数再生成」的草稿：任务页发起，生成页消费 */
  const [draft, setDraft] = useState<GenerateDraft | null>(null)
  /** 任务完成/失败的通知（notify.ts 全局 store，双轮询器按 key 去重） */
  const [toasts, setToasts] = useState<Toast[]>([])

  useEffect(() => subscribeToasts(setToasts), [])

  const refreshTemplates = useCallback(() => {
    getTemplates().then(setTemplates).catch(console.error)
  }, [])

  useEffect(() => {
    if (!getToken()) return
    refreshTemplates()
    const checkHealth = () => {
      getHealth()
        .then(setHealth)
        .catch(() => setHealth({ status: 'error', comfyui: 'unknown' }))
      // 引擎列表要登录态，失败就退回只看 ComfyUI（不让指示器整个消失）
      getEngines()
        .then(setEngines)
        .catch(() => setEngines(null))
    }
    checkHealth()
    // 健康状态每 30 秒刷新一次，离线恢复后可自动变绿
    const timer = setInterval(checkHealth, 30_000)
    return () => clearInterval(timer)
  }, [user, refreshTemplates])

  /** 全局任务完成检测（不随页面切换中断）：
   *  任务完成/失败的通知 + 素材库刷新信号都从这里发。
   *  之前挂在 TaskPanel/GenerateWorkspace 里，切到别的页组件卸载轮询就停了，
   *  导致「人在素材库时任务跑完」既不弹通知也不刷新素材。 */
  const prevTaskStatusRef = useRef<Map<string, string>>(new Map())
  useEffect(() => {
    if (!getToken()) return
    const checkTasks = async () => {
      try {
        const data = await listTasks()
        let changed = false
        for (const t of data) {
          const prev = prevTaskStatusRef.current.get(t.id)
          const wasActive = prev === 'pending' || prev === 'running'
          const nowTerminal =
            t.status === 'done' || t.status === 'error' || t.status === 'cancelled'
          if (wasActive && nowTerminal) {
            changed = true
            if (t.status === 'done') {
              notifyOnce(
                `done:${t.id}`,
                `任务完成：${String(t.params.prompt ?? t.params.description ?? '').slice(0, 20) || '生成任务'}`,
              )
            } else if (t.status === 'error') {
              notifyOnce(
                `error:${t.id}`,
                `任务失败：${friendlyError(t.error_msg || '').text.slice(0, 40)}`,
                'error',
              )
            }
          }
        }
        prevTaskStatusRef.current = new Map(data.map((t) => [t.id, t.status]))
        // 只在有任务跑到终态时才通知素材库刷新，避免空转
        if (changed) emitAssetsChanged()
      } catch {
        // 静默失败，下个轮询周期再试
      }
    }
    checkTasks()
    const timer = setInterval(checkTasks, 3000)
    return () => clearInterval(timer)
  }, [user])

  const handleLogout = () => {
    clearToken()
    setUser(null)
    setActiveKey('generate')
    setDraft(null)
  }

  /** 「用这些参数再生成」：把任务参数存成草稿，切到生成页预填 */
  const handleReuseTask = useCallback((task: Task) => {
    setDraft({
      template_id: task.template_id,
      params: task.params,
      source_task_id: task.id,
    })
    setActiveKey('generate')
  }, [])

  // 未登录 → 登录页
  if (!user || !getToken()) {
    return <Login onLogin={() => setUser(getStoredUser())} />
  }

  // 按注册表获取当前用户可见模块
  const visibleModules = getVisibleModules(user)
  const activeModule = visibleModules.find((m) => m.key === activeKey) ?? visibleModules[0]
  const ActiveComponent = activeModule?.component
  const online = health?.comfyui === 'connected'

  // 只看「有启用模板在用」的引擎：
  //  · generic_api 没配 base_url 时本来就是 disconnected，拿它报警等于天天误报
  //  · unknown（插件没实现 health_check）不算离线，那是「不知道」
  const engineIssues = useMemo(() => {
    if (!engines) return null
    const status = new Map(engines.map((e) => [e.name, e.status]))
    const used = new Set(templates.filter((t) => t.is_active).map((t) => t.engine))
    const missing: string[] = []
    const offline: string[] = []
    for (const name of used) {
      const s = status.get(name)
      if (s === undefined) missing.push(name)
      else if (s === 'disconnected' || s === 'error') offline.push(name)
    }
    return { missing, offline }
  }, [engines, templates])

  return (
    <div className="flex min-h-screen flex-col bg-paper text-ink">
      {/* 顶栏 — 一行品牌与身份，一行模块标签 */}
      <header className="rule-b bg-paper">
        <div className="container flex flex-wrap items-center justify-between gap-md py-sm">
          <div>
            <h1 className="font-display text-display">ComfyUI 工作台</h1>
            <p className="text-sm text-muted">AI 生成集成工作台</p>
          </div>

          <div className="flex items-center gap-lg">
            {/* 生成服务状态 — 文字本身说明状态，颜色只是复述。
                优先报「模板在用但连不上 / 没装」的引擎，都正常才退回 ComfyUI 状态 */}
            {engineIssues && (engineIssues.missing.length > 0 || engineIssues.offline.length > 0) ? (
              <span
                role="status"
                className="chip"
                data-tone="error"
                title={[
                  ...(engineIssues.offline.length
                    ? [`连不上：${engineIssues.offline.join('、')}`]
                    : []),
                  ...(engineIssues.missing.length
                    ? [`未安装：${engineIssues.missing.join('、')}`]
                    : []),
                ].join('\n')}
              >
                {engineIssues.missing.length > 0
                  ? `${engineIssues.missing.length} 个引擎未安装`
                  : `${engineIssues.offline.length} 个引擎离线`}
              </span>
            ) : health ? (
              <span
                role="status"
                className="chip"
                data-tone={online ? 'success' : 'error'}
              >
                {online ? '生成服务在线' : '生成服务离线'}
              </span>
            ) : null}

            <div className="flex items-center gap-md">
              <div>
                <div className="text-sm text-ink">
                  {user.display_name || user.username}
                </div>
                <div className="text-xs text-muted">
                  {user.role === 'admin' ? '管理员' : '成员'}
                </div>
              </div>
              <button
                onClick={() => setShowChangePassword(true)}
                className="btn-quiet"
                title="修改密码"
              >
                改密
              </button>
              <button
                onClick={handleLogout}
                className="btn-quiet"
                title="退出登录"
              >
                退出
              </button>
            </div>
          </div>
        </div>

        {/* Tab 切换 — 由模块注册表驱动 */}
        <nav aria-label="功能模块" className="rule-t">
          <div className="container flex gap-lg">
            {visibleModules.map((mod) => (
              <button
                key={mod.key}
                onClick={() => setActiveKey(mod.key)}
                aria-current={activeModule?.key === mod.key ? 'page' : undefined}
                className="tab"
              >
                {mod.label}
              </button>
            ))}
          </div>
        </nav>
      </header>

      {/* 主内容 — 渲染激活的模块组件 */}
      <main className="container flex-1 py-lg">
        {notice && (
          <p
            className="mb-md border border-rule bg-paper-2 p-sm text-sm text-ink"
            role="status"
          >
            {notice}
            <button className="btn-quiet ml-sm" onClick={() => setNotice('')}>
              知道了
            </button>
          </p>
        )}
        {ActiveComponent && (
          <ActiveComponent
            user={user}
            templates={templates}
            onTemplatesChange={refreshTemplates}
            onNavigate={setActiveKey}
            onReuseTask={handleReuseTask}
            draft={draft}
            onDraftConsumed={() => setDraft(null)}
          />
        )}
      </main>

      {/* 任务完成/失败通知 —— 右上角浮层，点击即关（6 秒后自动消失） */}
      {toasts.length > 0 && (
        <div
          className="fixed right-md top-md z-modal flex w-80 flex-col gap-xs"
          role="status"
          aria-live="polite"
        >
          {toasts.map((t) => (
            <button
              key={t.id}
              onClick={() => dismissToast(t.id)}
              className="border border-rule bg-paper p-sm text-left text-sm shadow-sm"
              data-tone={t.tone}
              title="点击关闭"
            >
              <span className="chip" data-tone={t.tone}>
                {t.tone === 'error' ? '失败' : '完成'}
              </span>
              <span className="ml-xs text-ink">{t.text}</span>
            </button>
          ))}
        </div>
      )}

      {/* 改密弹窗 — 任何角色都能改自己的密码 */}
      {showChangePassword && (
        <ChangePasswordModal
          onClose={() => setShowChangePassword(false)}
          onSuccess={() => setNotice('密码已修改，下次登录请用新密码')}
        />
      )}
    </div>
  )
}
