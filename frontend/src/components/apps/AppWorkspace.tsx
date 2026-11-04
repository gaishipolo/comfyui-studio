/** 「应用」模块外壳 —— 列表 ↔ 编辑器
 *
 * 一个搭好的流程就是一个应用：列表里挑一个，双击进去编排/调试/运行。
 * 数据模型仍然是 Flow（后端的 flow 表），这一层换的是**身份和入口**：
 * 以前是一个 Tab 里「左边一列流程 + 右边编排器」，现在是先选应用再进工作台。
 *
 * 为什么不新建一张 app 表：应用和流程此刻是同一份数据、同一套接口，
 * 多一张表只会多一次同步。等应用真的长出流程没有的东西（发布渠道、
 * 使用统计、独立权限），再拆不迟 —— 那时是从 flow 里抽，而不是重写。
 */
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import type { Flow, FlowRun, NodeCapability } from '../../types'
import {
  cancelFlowRun,
  createFlow,
  getFlowRun,
  importFlow,
  listFlows,
  listNodeCapabilities,
  runFlow,
} from '../../api/client'
import AppList from './AppList'
import AppEditor from './AppEditor'
import { notify, reportError } from '../../notify'
import { alertDialog, confirmDialog } from '../dialog'
import type { ModuleProps } from '../../modules/registry'

export default function AppWorkspace({ user }: ModuleProps) {
  const [view, setView] = useState<'list' | 'editor'>('list')
  const [flows, setFlows] = useState<Flow[]>([])
  const [caps, setCaps] = useState<NodeCapability[]>([])
  const [selectedId, setSelectedId] = useState('')
  const [run, setRun] = useState<FlowRun | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const pollRef = useRef<number | null>(null)
  const fitRef = useRef<HTMLDivElement | null>(null)
  const [fitHeight, setFitHeight] = useState<number | undefined>(undefined)

  /**
   * 工作台撑满可视区。
   *
   * 页面的高度是内容撑出来的（根节点只设了 min-height），所以 `h-full`
   * 在这里解析不出高度 —— 三栏会各自缩到内容高，右侧留白、左栏只有几个
   * 节点那么高。真要做工作台就只能按视口算一次。
   *
   * 量的是「这个容器距文档顶部的距离」，不是 scrollY —— 滚动时高度不该变。
   */
  useLayoutEffect(() => {
    const measure = () => {
      const el = fitRef.current
      if (!el) return
      const top = el.getBoundingClientRect().top + window.scrollY
      // 底部留 1rem 呼吸空间；下限 360px，避免窗口很矮时压成一条缝
      setFitHeight(Math.max(360, window.innerHeight - top - 16))
    }
    measure()
    window.addEventListener('resize', measure)
    return () => window.removeEventListener('resize', measure)
  }, [])

  const selected = useMemo(
    () => flows.find((f) => f.id === selectedId) ?? null,
    [flows, selectedId],
  )

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const [fs, cs] = await Promise.all([listFlows(), listNodeCapabilities()])
      setFlows(fs)
      setCaps(cs)
      // 用函数式更新读当前选中项 —— 把它放进依赖会造成「选中变化 → 重取 →
      // 选中被改 → 又重取」的循环，而每次重取都会把编辑器里没保存的改动冲掉
      setSelectedId((prev) => (fs.some((f) => f.id === prev) ? prev : (fs[0]?.id ?? '')))
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  // 运行中的流程：轮询进度直到终态
  useEffect(() => {
    if (pollRef.current !== null) {
      window.clearInterval(pollRef.current)
      pollRef.current = null
    }
    if (!run || run.status !== 'running') return
    pollRef.current = window.setInterval(() => {
      void (async () => {
        try {
          const next = await getFlowRun(run.id)
          setRun(next)
          if (next.status !== 'running' && pollRef.current !== null) {
            window.clearInterval(pollRef.current)
            pollRef.current = null
          }
        } catch {
          /* 轮询失败不打断运行 —— 下一次再试 */
        }
      })()
    }, 1500)
    return () => {
      if (pollRef.current !== null) {
        window.clearInterval(pollRef.current)
        pollRef.current = null
      }
    }
  }, [run])

  // 打开应用时清掉上一条运行记录 —— 否则会看到上一个应用在跑（或者更糟：
  // 以为这个应用正在跑）
  const openApp = (id: string) => {
    if (id !== selectedId) setRun(null)
    setSelectedId(id)
    setView('editor')
  }

  const handleRun = async (params: Record<string, unknown>) => {
    if (!selected) return
    try {
      const started = await runFlow(selected.id, params)
      setRun(started)
      notify(`应用「${selected.name}」已开始运行`, 'success')
    } catch (err) {
      reportError('运行失败', err)
    }
  }

  const handleCancel = async () => {
    if (!run) return
    try {
      const next = await cancelFlowRun(run.id)
      setRun(next)
      notify('已取消这次运行', 'info')
    } catch (err) {
      reportError('取消失败', err)
    }
  }

  const handleCreate = async () => {
    try {
      const created = await createFlow({
        name: '新应用',
        description: '',
        // **预置一个「提示词」输入** —— 九成的流程第一步就是「按提示词生成」。
        // 让用户先看到能填的东西，而不是一张空表再加字段。
        input_schema: {
          fields: [{ name: 'input1', label: '提示词', type: 'textarea', required: true }],
        },
        outputs: [],
        steps: [],
        status: 'active',
      })
      await load()
      setSelectedId(created.id)
      setRun(null)
      setView('editor')
      notify('已新建应用', 'success')
    } catch (err) {
      reportError('新建失败', err)
    }
  }

  /** 导入流程导出文件。只在真的重名时才问覆盖 —— 没冲突就不该拦一道 */
  const handleImport = async (file: File) => {
    let payload: unknown
    try {
      payload = JSON.parse(await file.text())
    } catch {
      notify('这个文件不是合法的 JSON', 'error')
      return
    }
    const fmt = (payload as { format?: string })?.format
    if (fmt !== 'comfyui-workbench/flow') {
      notify(`不是本系统导出的文件${fmt ? `（format=${fmt}）` : ''}`, 'error')
      return
    }
    const name =
      (payload as { flow?: { name?: string } })?.flow?.name || file.name.replace(/\.json$/i, '')
    const exists = flows.some((f) => f.name === name)
    let overwrite = false
    if (exists) {
      overwrite = await confirmDialog({
        title: `已存在同名应用「${name}」`,
        message: '覆盖会保留原 id，历史运行记录不受影响。',
        confirmText: '覆盖',
        cancelText: '取消',
      })
      if (!overwrite) return
    }
    try {
      const res = await importFlow(payload, overwrite)
      await load()
      setSelectedId(res.id)
      if (res.warnings.length) {
        await alertDialog({
          title: `已导入「${res.name}」`,
          message: res.warnings.map((w) => `· ${w}`).join('\n'),
          tone: 'warning',
        })
      } else {
        notify(`已导入「${res.name}」`, 'success')
      }
    } catch (err) {
      reportError('导入失败', err)
    }
  }

  return (
    <div
      ref={fitRef}
      className="flex min-h-0 flex-col gap-sm"
      style={fitHeight === undefined ? undefined : { height: fitHeight }}
      data-app-workspace={view}
    >
      {error && <p className="text-sm text-error">{error}</p>}
      {view === 'editor' && selected ? (
        <AppEditor
          flow={selected}
          flows={flows}
          caps={caps}
          user={user}
          run={run}
          onBack={() => {
            setView('list')
            setRun(null)
          }}
          onChanged={load}
          onSelect={setSelectedId}
          onRun={(params) => void handleRun(params)}
          onCancelRun={() => void handleCancel()}
          onRunStarted={setRun}
        />
      ) : (
        <AppList
          apps={flows}
          loading={loading}
          selectedId={selectedId}
          onSelect={setSelectedId}
          onOpen={openApp}
          onCreate={() => void handleCreate()}
          onImport={(file) => void handleImport(file)}
          canImport={user.role === 'admin'}
        />
      )}
    </div>
  )
}
