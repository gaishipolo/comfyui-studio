/** 场景选择 — 卡片网格，当前选中由强调色描边标记 */
import type { Template } from '../types'

interface Props {
  templates: Template[]
  selected: Template | null
  onSelect: (template: Template) => void
}

/** 按分类给一枚图标：形状区分类型，颜色只表达「选中 / 未选中」，
 *  不额外引入第二套配色 —— 强调色仍然是唯一的彩色支出点。 */
const CATEGORY_ICON: Record<string, string> = {
  image: 'M4 5h16v14H4zM9.5 10.5a1.5 1.5 0 1 0 0-3 1.5 1.5 0 0 0 0 3M20 16l-4.5-4.5L7 20',
  video: 'M4 6h11v12H4zM15 10.5l5-3.5v10l-5-3.5',
  audio: 'M9 18V5l10-2v13M9 18a2.5 2.5 0 1 1-5 0 2.5 2.5 0 0 1 5 0M19 16a2.5 2.5 0 1 1-5 0 2.5 2.5 0 0 1 5 0',
  text: 'M5 6h14M12 6v13M9 19h6',
}
const DEFAULT_ICON = 'M4 4h16v16H4z'

export default function TemplateGallery({ templates, selected, onSelect }: Props) {
  return (
    <section>
      <div className="mb-md flex items-end justify-between gap-sm">
        <div>
          <h2 className="section-title">选择生成场景</h2>
          <p className="section-hint mt-2xs">选中后右侧出现对应的输入表单</p>
        </div>
        {templates.length > 0 && (
          <span className="shrink-0 pb-2xs text-xs text-muted">{templates.length} 个场景</span>
        )}
      </div>

      {templates.length === 0 ? (
        <div className="empty">
          <span className="empty-icon" aria-hidden="true">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none">
              <path
                d="M4 5h16v14H4zM9.5 10.5a1.5 1.5 0 1 0 0-3 1.5 1.5 0 0 0 0 3M20 16l-4.5-4.5L7 20"
                stroke="currentColor"
                strokeWidth="1.7"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
          </span>
          <p className="empty-title">还没有可用的生成场景</p>
          <p className="empty-hint">请管理员在「管理」页启用一个场景模板</p>
        </div>
      ) : (
        <ul className="scroll-thin grid max-h-[min(62vh,36rem)] gap-sm overflow-y-auto pr-2xs sm:grid-cols-2">
          {templates.map((t) => {
            const on = selected?.id === t.id
            const icon = CATEGORY_ICON[t.category] ?? DEFAULT_ICON
            return (
              <li key={t.id}>
                <button
                  type="button"
                  onClick={() => onSelect(t)}
                  aria-pressed={on}
                  className={`card card-hover flex h-full w-full flex-col p-sm text-left ${
                    on ? 'border-accent bg-accent-soft' : ''
                  }`}
                >
                  <span className="flex items-start gap-xs">
                    <span
                      aria-hidden="true"
                      className={`grid h-8 w-8 shrink-0 place-items-center rounded-sm transition-colors duration-micro ${
                        on ? 'bg-accent text-accent-ink' : 'bg-paper-2 text-ink-2'
                      }`}
                    >
                      <svg width="16" height="16" viewBox="0 0 24 24" fill="none">
                        <path
                          d={icon}
                          stroke="currentColor"
                          strokeWidth="1.7"
                          strokeLinecap="round"
                          strokeLinejoin="round"
                        />
                      </svg>
                    </span>

                    <span className="min-w-0 flex-1 pt-2xs">
                      <span
                        className={`block truncate font-display text-title font-semibold ${
                          on ? 'text-accent' : 'text-ink'
                        }`}
                        title={t.name}
                      >
                        {t.name}
                      </span>
                      <span className="mt-2xs line-clamp-2 min-h-[2.6em] text-sm leading-snug text-muted">
                        {t.description || '　'}
                      </span>
                    </span>

                    <span
                      aria-hidden="true"
                      className={`mt-2xs grid h-4 w-4 shrink-0 place-items-center rounded-pill border text-[10px] leading-none transition-colors duration-micro ${
                        on
                          ? 'border-accent bg-accent text-accent-ink'
                          : 'border-rule-strong bg-paper text-transparent'
                      }`}
                    >
                      ✓
                    </span>
                  </span>
                </button>
              </li>
            )
          })}
        </ul>
      )}
    </section>
  )
}
