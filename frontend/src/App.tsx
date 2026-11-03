/**
 * 主应用 — 认证 + 按模块注册表动态渲染
 *
 * 所有功能 Tab 由 modules/ 注册表驱动，新增模块无需改本文件。
 */
import { useState, useEffect, useCallback, useRef } from 'react'
import type { EngineInfo, GenerateDraft, Task, Template, User } from './types'
import {
  getTemplates, getHealth, getEngines, listTasks,
  getToken, getStoredUser, clearToken,
} from './api/client'
import { getVisibleModules } from './modules'
import {
  subscribe as subscribeToasts, dismiss as dismissToast,
  notifyOnce, emitAssetsChanged, resetNotifications,
} from './notify'
import { friendlyError } from './components/FriendlyError'
import { clearTaskStatusHistory, taskPollDelay } from './components/uiBehavior'
import type { Toast } from './notify'
import Login from './components/Login'
import ChangePasswordModal from './components/ChangePasswordModal'
import DialogHost from './components/DialogHost'

/** 模块图标（24 视框线性图）。按注册表 key 取，未登记的模块退回一个通用方块 */
const MODULE_ICON: Record<string, string> = {
  generate: 'M12 3.5 13.9 9l5.6 1.9-5.6 1.9L12 18.4l-1.9-5.6L4.5 10.9 10.1 9 12 3.5Z',
  tasks: 'M8 6h12M8 12h12M8 18h12M4 6h.01M4 12h.01M4 18h.01',
  assets: 'M4 5h16v14H4zM9.5 10.5a1.5 1.5 0 1 0 0-3 1.5 1.5 0 0 0 0 3M20 16l-4.5-4.5L7 20',
  admin: 'M4 6h16M4 12h10M4 18h6',
  users:
    'M15 20v-1.5a4 4 0 0 0-4-4H7a4 4 0 0 0-4 4V20M9 10.5a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7M21 20v-1.5a4 4 0 0 0-3-3.87M16.5 3.6a3.5 3.5 0 0 1 0 6.8',
  usage: 'M4 20V11M10 20V4M16 20v-6M22 20H2',
  plugins: 'M9 3v5M15 3v5M6 8h12v3a6 6 0 0 1-12 0zM12 17v4',
}
const DEFAULT_ICON = 'M4 4h16v16H4z'

/** toast 的三种外观：左侧色条、图标色、图标路径 */
const TOAST_BAR: Record<Toast['tone'], string> = {
  success: 'bg-success',
  error: 'bg-error',
  warning: 'bg-warning',
  info: 'bg-accent',
}
const TOAST_INK: Record<Toast['tone'], string> = {
  success: 'text-success',
  error: 'text-error',
  warning: 'text-warning',
  info: 'text-accent',
}
const TOAST_ICON: Record<Toast['tone'], string> = {
  success: 'M5 13l4 4L19 7',
  error: 'M7 7l10 10M17 7L7 17',
  warning: 'M12 8v5m0 3h.01',
  info: 'M12 11v6m0-9h.01',
}

export default function App() {
  const [user, setUser] = useState<User | null>(getStoredUser())
  const [templates, setTemplates] = useState<Template[]>([])
  const [health, setHealth] = useState<{ status: string; comfyui: string } | null>(null)
  /** 各引擎的健康状态。多引擎之后光看 ComfyUI 会骗人 ——
   *  用户选的可能是 grsai，它挂了顶部还显示「在线」，然后任务莫名失败 */
  const [engines, setEngines] = useState<EngineInfo[] | null>(null)
  const [activeKey, setActiveKey] = useState('generate')
  const [showChangePassword, setShowChangePassword] = useState(false)
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
    let stopped = false
    let timer: ReturnType<typeof setTimeout> | undefined
    const checkTasks = async () => {
      let nextDelay = 15_000
      try {
        const data = await listTasks()
        nextDelay = taskPollDelay(data)
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
        nextDelay = 15_000
      } finally {
        if (!stopped) timer = setTimeout(checkTasks, nextDelay)
      }
    }
    checkTasks()
    return () => {
      stopped = true
      if (timer) clearTimeout(timer)
    }
  }, [user])

  const handleLogout = () => {
    clearToken()
    resetNotifications()
    clearTaskStatusHistory(prevTaskStatusRef.current)
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
  //  · 没配 base_url 的引擎本来就是 disconnected，拿它报警等于天天误报
  //  · unknown（插件没实现 health_check）不算离线，那是「不知道」
  const engineIssues = (() => {
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
  })()

  return (
    <div className="flex min-h-screen flex-col text-ink">
      {/* 顶栏 — 一行品牌与身份，一行模块胶囊 */}
      <header className="sticky top-0 z-sticky-nav border-b border-rule bg-paper">
        <div className="container flex flex-wrap items-center justify-between gap-md py-sm">
          <div className="flex items-center gap-sm">
            {/* 品牌标记：唯一一处渐变，比纯色方块更有"产品感" */}
            <span
              aria-hidden="true"
              className="grid h-9 w-9 shrink-0 place-items-center rounded-md bg-[linear-gradient(135deg,#6d6df0_0%,#5b5bd6_52%,#4646b8_100%)] text-accent-ink shadow-sm"
            >
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden="true">
                <path
                  d="M12 3.5 20 8v8l-8 4.5L4 16V8l8-4.5Z"
                  stroke="currentColor"
                  strokeWidth="1.6"
                  strokeLinejoin="round"
                />
                <circle cx="12" cy="12" r="2.6" fill="currentColor" />
              </svg>
            </span>
            <div className="min-w-0">
              <h1 className="font-display text-xl font-semibold leading-tight tracking-tight">
                ComfyUI 工作台
              </h1>
              <p className="text-xs text-muted">AI 生成集成工作台</p>
            </div>
          </div>

          <div className="flex items-center gap-md">
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
                <span className="h-1.5 w-1.5 rounded-pill bg-error" aria-hidden="true" />
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
                <span
                  className={`h-1.5 w-1.5 rounded-pill ${online ? 'bg-success' : 'bg-error'}`}
                  aria-hidden="true"
                />
                {online ? '生成服务在线' : '生成服务离线'}
              </span>
            ) : null}

            <div className="flex items-center gap-sm border-l border-rule pl-md">
              <span
                aria-hidden="true"
                className="grid h-8 w-8 shrink-0 place-items-center rounded-pill bg-paper-2 text-xs font-semibold uppercase text-ink-2 ring-1 ring-inset ring-rule"
              >
                {(user.display_name || user.username).trim().slice(0, 1)}
              </span>
              <div className="leading-tight">
                <div className="text-sm font-medium text-ink">
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
        <nav aria-label="功能模块" className="container overflow-x-auto pb-sm">
          <div className="inline-flex gap-2xs rounded-md bg-paper-2 p-1">
            {visibleModules.map((mod) => (
              <button
                key={mod.key}
                onClick={() => setActiveKey(mod.key)}
                aria-current={activeModule?.key === mod.key ? 'page' : undefined}
                className="tab"
              >
                <svg
                  width="16"
                  height="16"
                  viewBox="0 0 24 24"
                  fill="none"
                  className="shrink-0"
                  aria-hidden="true"
                >
                  <path
                    d={MODULE_ICON[mod.key] ?? DEFAULT_ICON}
                    stroke="currentColor"
                    strokeWidth="1.7"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                  />
                </svg>
                {mod.label}
              </button>
            ))}
          </div>
        </nav>
      </header>

      {/* 主内容 — 渲染激活的模块组件 */}
      <main className="container flex-1 py-lg">
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

      {/* 任务完成/失败通知 —— 右下角浮层，点击即关。
          不用右上角：顶栏是 sticky 的，通知会压在状态 chip 和用户区上。
          `flex-col-reverse` 让最新的一条贴着屏幕底部（视觉上最靠下 = 最新）。 */}
      {toasts.length > 0 && (
        <div
          className="fixed bottom-md right-md z-toast flex w-80 flex-col-reverse gap-xs"
          role="status"
          aria-live="polite"
        >
          {toasts.map((t) => (
            <button
              key={t.id}
              onClick={() => dismissToast(t.id)}
              className="relative flex items-start gap-xs overflow-hidden rounded-md border border-rule bg-paper p-sm pr-md text-left text-sm shadow-lg transition-shadow duration-micro hover:border-rule-strong"
              title="点击关闭"
            >
              {/* 左侧色条：一眼分辨成功/失败，不用读字 */}
              <span
                aria-hidden="true"
                className={`absolute inset-y-0 left-0 w-1 ${TOAST_BAR[t.tone]}`}
              />
              <span
                aria-hidden="true"
                className={`mt-2xs grid h-4 w-4 shrink-0 place-items-center ${TOAST_INK[t.tone]}`}
              >
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" aria-hidden="true">
                  <path
                    d={TOAST_ICON[t.tone]}
                    stroke="currentColor"
                    strokeWidth="2.2"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                  />
                </svg>
              </span>
              <span className="min-w-0 flex-1 text-ink">{t.text}</span>
            </button>
          ))}
        </div>
      )}

      {/* 全局对话框 —— 替代原生 alert / confirm */}
      <DialogHost />

      {/* 改密弹窗 — 任何角色都能改自己的密码 */}
      {showChangePassword && (
        <ChangePasswordModal
          onClose={() => setShowChangePassword(false)}
          onSuccess={handleLogout}
        />
      )}
    </div>
  )
}
