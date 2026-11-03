/** 节点参数弹窗 —— 双击节点卡片打开
 *
 * 主区（节点列表）只显示「谁 + 吃什么」，细节收在这里。理由跟 LibTV 那类
 * 画布工具一样：流程步骤一多，摊开的参数行会把整页塞满，扫不动了。
 *
 * 「这一步吃什么」是参数（字面量或 {{...}} 引用）；「产出什么」由节点模板
 * 决定，这里只显示「谁在吃它的产物」，让人知道这条线接到哪里去了。
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import type { FlowStep, NodeCapability, TemplateField } from '../types'
import { useModalKeyboard } from './useModalKeyboard'
import { nodeVisual, paramSummary, stepDependencies } from './flowEditor'

interface Props {
  step: FlowStep
  steps: FlowStep[]
  caps: NodeCapability[]
  inputFields: TemplateField[]
  onSave: (step: FlowStep) => void
  onDelete: () => void
  onClose: () => void
}

export default function StepEditorDialog({
  step,
  steps,
  caps,
  inputFields,
  onSave,
  onDelete,
  onClose,
}: Props) {
  const [draft, setDraft] = useState<FlowStep>(step)
  const dialogRef = useRef<HTMLDivElement>(null)
  useModalKeyboard(true, dialogRef, onClose)

  useEffect(() => {
    setDraft(step)
  }, [step])

  const cap = useMemo(
    () => caps.find((c) => c.id === draft.template_id) ?? null,
    [caps, draft.template_id],
  )
  const visual = nodeVisual(cap)
  const upstreams = steps.filter((s) => s.id !== draft.id)
  // 谁在吃这一步的产物
  const consumers = steps.filter((s) => stepDependencies(steps, s.id).includes(draft.id))

  const patch = (part: Partial<FlowStep>) => setDraft((d) => ({ ...d, ...part }))

  const setParam = (name: string, value: string) =>
    patch({ params: { ...draft.params, [name]: value } })

  /**
   * 按字段类型给对应的控件 —— 下拉就别让用户手打值（「2K」打成「2k」
   * 后端会当成非法值）。控件类型和 DynamicForm 里一致。
   */
  const renderField = (
    f: TemplateField,
    value: string,
    onChange: (v: string) => void,
  ) => {
    const placeholder =
      f.type === 'file_upload' ? '素材 id，或 {{ steps.x.outputs }}' : '字面量，或 {{ input.xxx }}'
    if (f.type === 'select' && f.options?.length) {
      return (
        <select
          className="field"
          value={value}
          onChange={(e) => onChange(e.target.value)}
        >
          <option value="">（不设置）</option>
          {f.options.map((o) => (
            <option key={String(o)} value={String(o)}>
              {String(o)}
            </option>
          ))}
        </select>
      )
    }
    if (f.type === 'number' || f.type === 'random_number') {
      return (
        <input
          className="field"
          type="number"
          value={value}
          placeholder={placeholder}
          onChange={(e) => onChange(e.target.value)}
        />
      )
    }
    if (f.type === 'textarea') {
      return (
        <textarea
          className="field"
          rows={3}
          value={value}
          placeholder={placeholder}
          onChange={(e) => onChange(e.target.value)}
        />
      )
    }
    return (
      <input
        className="field"
        value={value}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
      />
    )
  }

  return (
    <div
      className="fixed inset-0 z-modal flex items-center justify-center bg-scrim p-md"
      onClick={onClose}
    >
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="step-editor-title"
        tabIndex={-1}
        className="flex max-h-[85vh] w-full max-w-2xl flex-col overflow-hidden rounded-lg border border-rule bg-paper shadow-lg"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between gap-xs border-b border-rule p-md">
          <div className="flex min-w-0 items-center gap-sm">
            <span aria-hidden="true" className="text-ink-2">
              {visual.icon}
            </span>
            <h3 id="step-editor-title" className="truncate font-display text-title">
              {cap?.name ?? '节点设置'}
            </h3>
            <code className="text-xs text-muted">{draft.id}</code>
          </div>
          <button type="button" onClick={onClose} className="x-btn" aria-label="关闭节点设置">
            ✕
          </button>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto p-md">
          <div className="flex flex-wrap items-end gap-sm">
            <label className="field-label w-40">
              步骤名
              <input
                className="field"
                value={draft.id}
                onChange={(e) => patch({ id: e.target.value })}
                autoFocus
              />
            </label>
            <label className="field-label flex-1">
              节点
              <select
                className="field"
                value={draft.template_id}
                onChange={(e) => {
                  const next = caps.find((c) => c.id === e.target.value)
                  patch({
                    template_id: e.target.value,
                    // 换节点后字段全变了，参数按同名字段保留、其余清掉
                    params: next
                      ? Object.fromEntries(
                          next.fields.map((f) => [
                            f.name,
                            draft.params?.[f.name] ?? '',
                          ]),
                        )
                      : {},
                  })
                }}
              >
                <option value="">选一个节点…</option>
                {caps.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.name}
                  </option>
                ))}
              </select>
            </label>
          </div>

          <h4 className="mt-md font-display text-base font-medium">这一步吃什么</h4>
          {!cap ? (
            <p className="mt-2xs text-sm text-muted">先选一个节点</p>
          ) : cap.fields.length === 0 ? (
            <p className="mt-2xs text-sm text-muted">这个节点没有可填的参数</p>
          ) : (
            <div className="mt-sm flex flex-col gap-sm">
              {cap.fields.map((f) => (
                <div key={f.name} className="flex flex-wrap items-end gap-xs">
                  <label className="field-label min-w-40 flex-1">
                    <span className="text-xs text-muted">
                      {f.label || f.name}
                      {f.required ? ' *' : ''}
                      {f.type === 'file_upload' && (
                        <span className="ml-2xs">（接素材或上游产物）</span>
                      )}
                    </span>
                    {renderField(f, draft.params?.[f.name] ?? '', (v) => setParam(f.name, v))}
                  </label>
                  {draft.params?.[f.name] ? null : (
                    <select
                      className="field w-52"
                      value=""
                      aria-label={`为「${f.label || f.name}」插入引用`}
                      onChange={(e) => {
                        if (e.target.value) setParam(f.name, e.target.value)
                      }}
                    >
                      <option value="">插入引用…</option>
                      {inputFields.map((inp) => (
                        <option key={`in-${inp.name}`} value={`{{ input.${inp.name} }}`}>
                          流程输入 · {inp.label || inp.name}
                        </option>
                      ))}
                      {upstreams.map((u) => (
                        <option key={`up-${u.id}`} value={`{{ steps.${u.id}.outputs }}`}>
                          上游产物 · {u.id}
                        </option>
                      ))}
                    </select>
                  )}
                </div>
              ))}
            </div>
          )}

          <h4 className="mt-lg font-display text-base font-medium">这一步产出什么</h4>
          <p className="mt-2xs text-sm text-muted">
            产出由节点决定（{cap ? `${cap.name} 的输出` : '选好节点后可知'}）。
            {consumers.length > 0
              ? ` 下面这些步骤在吃它的产物：${consumers.map((c) => c.id).join('、')}`
              : ' 目前没有别的步骤用它的产物。'}
          </p>
          {draft.params && Object.keys(draft.params).length > 0 && (
            <p className="mt-2xs text-xs text-muted">
              当前设置：{paramSummary(draft, cap)}
            </p>
          )}
        </div>

        <div className="flex items-center justify-between gap-sm border-t border-rule p-md">
          <button type="button" className="btn btn-danger-quiet" onClick={onDelete}>
            删除这一步
          </button>
          <div className="flex gap-sm">
            <button type="button" className="btn-quiet" onClick={onClose}>
              取消
            </button>
            <button type="button" className="btn btn-primary" onClick={() => onSave(draft)}>
              应用
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}
