/** 应用列表 —— 一个搭好的流程就是一个应用
 *
 * 这一层只做「选哪个应用」：卡片网格，**双击打开**，单击选中。
 * 打开之后是应用编辑器（左节点/素材 · 中步骤 · 右子流程/agent）。
 *
 * 为什么把流程叫应用：对用的人来说，「流程」是实现细节（几个节点怎么串），
 * 「应用」才是他拿到的东西（填两个框，点一下，出图）。数据还是同一份，
 * 换的是呈现的身份 —— 所以没有新表，也没有第二套接口。
 */
import type { Flow } from '../../types'

const STATUS_LABEL: Record<string, string> = {
  active: '已启用',
  draft: '待审批',
  archived: '已归档',
}

/** 只有「待审批」需要跳出来提醒 —— 已启用是常态，归档本来就该淡出 */
const STATUS_TONE: Record<string, string | undefined> = {
  draft: 'accent',
}

/** 应用卡片的图标：优先用自定义 emoji，其次取名字首字 */
function appGlyph(app: Flow): string {
  const icon = (app.icon ?? '').trim()
  if (icon) return icon
  const name = (app.name ?? '').trim()
  return name ? [...name][0] : '·'
}

export default function AppList({
  apps,
  loading,
  selectedId,
  onSelect,
  onOpen,
  onCreate,
  onImport,
  canImport,
}: {
  apps: Flow[]
  loading: boolean
  selectedId: string
  onSelect: (id: string) => void
  /** 打开应用编辑器 */
  onOpen: (id: string) => void
  onCreate: () => void
  onImport: (file: File) => void
  canImport: boolean
}) {
  return (
    <div className="flex h-full min-h-0 flex-col gap-md">
      <div className="flex flex-wrap items-end justify-between gap-sm">
        <div>
          <h2 className="font-display text-lg font-semibold">应用</h2>
          <p className="text-xs text-muted">
            一个搭好的流程就是一个应用。双击打开，编排、调试、运行都在里面。
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-xs">
          {canImport && (
            <label className="btn btn-quiet cursor-pointer">
              导入
              <input
                type="file"
                accept=".json,application/json"
                className="hidden"
                onChange={(e) => {
                  const file = e.target.files?.[0]
                  e.target.value = '' // 允许重复导入同一个文件
                  if (file) onImport(file)
                }}
              />
            </label>
          )}
          <button type="button" className="btn btn-primary" onClick={onCreate}>
            新建应用
          </button>
        </div>
      </div>

      {loading && apps.length === 0 ? (
        <p className="text-sm text-muted">加载中…</p>
      ) : apps.length === 0 ? (
        <div className="empty">
          <p className="empty-title">还没有应用</p>
          <p className="empty-hint">
            新建一个空应用，进去之后可以用左栏的节点手工搭，也可以用右栏的
            agent 让它先搭个草稿
          </p>
        </div>
      ) : (
        <div className="min-h-0 flex-1 overflow-y-auto">
          <ul className="grid grid-cols-1 gap-sm sm:grid-cols-2 xl:grid-cols-3">
            {apps.map((app) => {
              const active = selectedId === app.id
              return (
                <li key={app.id}>
                  <div
                    // 双击打开是用户画的交互；键盘用 Enter，鼠标有「打开」按钮 ——
                    // 只靠双击等于把键盘用户挡在外面
                    role="button"
                    tabIndex={0}
                    data-app-card={app.id}
                    aria-label={`应用 ${app.name}`}
                    onDoubleClick={() => onOpen(app.id)}
                    onClick={() => onSelect(app.id)}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter' || e.key === ' ') {
                        e.preventDefault()
                        onOpen(app.id)
                      }
                    }}
                    className={`group flex h-full cursor-pointer flex-col gap-xs rounded-lg border p-sm text-left transition-colors ${
                      active
                        ? 'border-accent bg-secondary'
                        : 'border-tertiary hover:border-rule-strong hover:bg-secondary'
                    }`}
                  >
                    <div className="flex items-start gap-sm">
                      <span
                        aria-hidden="true"
                        className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border border-rule bg-paper-2 text-base"
                      >
                        {appGlyph(app)}
                      </span>
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-xs">
                          <span className="truncate font-medium">{app.name}</span>
                          <span
                            className="chip shrink-0"
                            data-tone={STATUS_TONE[app.status]}
                          >
                            {STATUS_LABEL[app.status] ?? app.status}
                          </span>
                        </div>
                        <p className="mt-2xs line-clamp-2 text-xs text-muted">
                          {app.description || '还没有说明'}
                        </p>
                      </div>
                    </div>

                    <div className="mt-auto flex items-center justify-between gap-sm pt-2xs">
                      <span className="text-xs text-muted">
                        {app.steps.length} 步
                        {app.outputs.length > 0 ? ` · ${app.outputs.length} 项产出` : ''}
                      </span>
                      <button
                        type="button"
                        className="btn btn-quiet opacity-0 transition-opacity group-hover:opacity-100 focus:opacity-100"
                        onClick={(e) => {
                          e.stopPropagation()
                          onOpen(app.id)
                        }}
                      >
                        打开
                      </button>
                    </div>
                  </div>
                </li>
              )
            })}
          </ul>
          <p className="mt-sm text-xs text-muted">双击卡片打开应用。</p>
        </div>
      )}
    </div>
  )
}
