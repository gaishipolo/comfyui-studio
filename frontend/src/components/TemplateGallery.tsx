/** 场景选择 — 细线分隔的行列表，当前选中由强调色标记 */
import type { Template } from '../types'

interface Props {
  templates: Template[]
  selected: Template | null
  onSelect: (template: Template) => void
}

export default function TemplateGallery({ templates, selected, onSelect }: Props) {
  return (
    <section>
      <h2 className="font-display text-section">选择生成场景</h2>

      {templates.length === 0 ? (
        <p className="mt-md border-t border-rule py-lg text-base text-muted">
          还没有可用的生成场景
        </p>
      ) : (
        <ul className="mt-sm border-t border-rule">
          {templates.map((t) => (
            <li key={t.id}>
              <button
                type="button"
                onClick={() => onSelect(t)}
                aria-pressed={selected?.id === t.id}
                className="select-row"
              >
                <span
                  className={
                    selected?.id === t.id
                      ? 'block font-display text-title text-accent'
                      : 'block font-display text-title text-ink'
                  }
                >
                  {t.name}
                </span>
                <span className="mt-2xs block text-sm text-muted">{t.description}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
