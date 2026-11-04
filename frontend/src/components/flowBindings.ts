/** 自动接上（同名绑定）的规则 —— 与后端 `flow_bindings.py` 逐字对应
 *
 * **为什么需要它**：节点参数不配也能跑，是这套系统「不用教用户写表达式」的
 * 根本。但「不配 = 接什么」必须有唯一答案 —— 否则界面显示的和实际跑的不是
 * 一回事，而这种错不会报错，只会让用户拿到错的图。
 *
 * **规则（两条，按顺序）**：
 * 1. 节点字段的**标签**与某个流程输入字段的**标签**相同 → 用那个流程输入
 * 2. 节点字段的**标签**与某个上游步骤的**产出名**相同 → 用那个步骤的产物
 *    （只有文件/图片类字段能接产物 —— 文本字段喂产物列表没有意义）
 *
 * **为什么按标签而不是内部标识**：流程输入的 name 是系统生成的（input1、
 * input2…），节点字段的 name 是英文（prompt、image…）。两边内部标识基本
 * 对不上，但**标签都是人写的、而且多半是同一个词**（「提示词」「参考图」）。
 *
 * 后端对应 `backend/app/services/flow_bindings.py`，两边规则逐字相同，
 * 契约清单见 `backend/tests/test_flow_bindings.py` 与
 * `frontend/tests/flow_bindings.test.ts`。
 */
import type { TemplateField } from '../types'

/** 两个东西是不是「同名」。标签有值时只比标签。 */
export function sameName(
  aLabel: string | undefined | null,
  aName: string | undefined | null,
  bLabel: string | undefined | null,
  bName: string | undefined | null,
): boolean {
  if (aLabel && bLabel) return aLabel === bLabel
  if (aName && bName) return aName === bName
  return false
}

/** 这个字段该接哪个流程输入。接不上返回 null。 */
export function matchInput(
  field: { name: string; label?: string },
  inputFields: TemplateField[],
): TemplateField | null {
  for (const f of inputFields) {
    if (sameName(field.label, field.name, f.label, f.name)) return f
  }
  return null
}

/** 这个字段该接哪个上游步骤的产物。接不上返回 null。 */
export function matchUpstream(
  field: { name: string; label?: string },
  upstreamOutputs: Record<string, string>,
): { name: string; stepId: string } | null {
  for (const [name, stepId] of Object.entries(upstreamOutputs)) {
    if (sameName(field.label, field.name, name, name)) return { name, stepId }
  }
  return null
}

/** 自动接上的结果 */
export type AutoBinding =
  | { kind: 'input'; field: TemplateField }
  | { kind: 'upstream'; name: string; stepId: string }

/**
 * 这个字段自动会接到什么。接不上返回 null（那就不传，用节点自己的默认值）。
 *
 * upstreamOutputs: {产出名: 步骤 id} —— 只放**前面**的步骤。
 */
export function autoBinding(
  field: { name: string; label?: string; type?: string },
  inputFields: TemplateField[],
  upstreamOutputs: Record<string, string>,
): AutoBinding | null {
  const src = matchInput(field, inputFields)
  if (src) return { kind: 'input', field: src }

  const hit = matchUpstream(field, upstreamOutputs)
  if (!hit) return null
  // 文本字段接不了产物（喂一堆素材路径给文本框没有意义）
  if (field.type !== 'file_upload' && field.type !== 'image_upload') return null
  return { kind: 'upstream', name: hit.name, stepId: hit.stepId }
}

/**
 * 这一步的产出叫什么。默认取步骤标题 —— 用户不改也能用：
 * 下游节点的字段标签跟步骤标题同名就接得上。
 */
export function stepOutputName(step: {
  output_name?: string
  title?: string
}): string {
  return (step.output_name ?? step.title ?? '').trim()
}
