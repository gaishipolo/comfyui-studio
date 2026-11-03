/** 流程编排器的纯逻辑 —— 不碰 React，好测
 *
 * 界面形态参考 LibTV 那类画布工具：主区是一列节点卡片，每张卡片只显示
 * 「谁 + 吃什么」，细节收进弹窗。主区一列的原因是：流程天然是有序的，
 * 顺序本身就是信息 —— 摊成自由画布反而要靠连线去表达顺序。
 *
 * 这里放三件事：
 * 1. 节点卡片的图标与摘要（列表项长什么样）
 * 2. 步骤间的依赖解析（谁吃了谁的产物）
 * 3. 拖动排序的合法性 —— **拖动不能破坏依赖**
 */
import type { FlowStep, NodeCapability, TemplateField } from '../types'
import { describeParam, parseParamSource } from './flowSource'

/** 引用上游产物的写法：{{ steps.s1.outputs }} */
const STEP_REF = /\{\{\s*steps\.([A-Za-z0-9_]+)\.outputs\s*\}\}/g

/** 这一步依赖哪些上游步骤的产物 */
export function stepDependencies(steps: FlowStep[], stepId: string): string[] {
  const step = steps.find((s) => s.id === stepId)
  if (!step) return []
  const deps = new Set<string>()
  for (const expr of Object.values(step.params ?? {})) {
    if (typeof expr !== 'string') continue
    for (const m of expr.matchAll(STEP_REF)) {
      if (m[1] !== stepId) deps.add(m[1])
    }
  }
  return [...deps]
}

/** 依赖是否被破坏了：引用了上游，却排在上游前面 */
export function isOrderValid(steps: FlowStep[]): boolean {
  const pos = new Map(steps.map((s, i) => [s.id, i]))
  return steps.every((s) => {
    const at = pos.get(s.id)
    if (at === undefined) return false
    return stepDependencies(steps, s.id).every((dep) => {
      const p = pos.get(dep)
      return p !== undefined && p < at
    })
  })
}

/** 移动一步，返回新数组 */
export function reorderSteps(steps: FlowStep[], fromIdx: number, toIdx: number): FlowStep[] {
  const next = [...steps]
  const [moved] = next.splice(fromIdx, 1)
  if (!moved) return steps
  next.splice(Math.max(0, Math.min(next.length, toIdx)), 0, moved)
  return next
}

/**
 * 这次拖动会不会破坏依赖。
 *
 * 破坏了就别动 —— 让用户在拖动之前就知道动不了，而不是拖完再告诉他
 * 「你把流程搞坏了」。后端校验会报同样的错，但那时已经改了草稿。
 */
export function wouldBreakDependency(steps: FlowStep[], fromIdx: number, toIdx: number): boolean {
  if (fromIdx === toIdx || fromIdx < 0 || toIdx < 0) return false
  return !isOrderValid(reorderSteps(steps, fromIdx, toIdx))
}

/** 节点卡片左侧的图标与配色 —— 按能力/引擎猜，让列表一眼能扫 */
export interface NodeVisual {
  icon: string
  tone: 'image' | 'video' | 'audio' | 'text' | 'generic'
}

/** 能力 kind 的最后一段是产出类型：text_to_image → image、image_to_video → video */
const OUTPUT_KINDS = new Set(['image', 'video', 'audio', 'text'])

export function nodeVisual(cap: NodeCapability | null): NodeVisual {
  if (!cap) return { icon: '◇', tone: 'generic' }

  const kind = cap.capability || ''
  const segs = kind.split('_')
  const outKind = segs[segs.length - 1]
  if (OUTPUT_KINDS.has(outKind)) {
    if (outKind === 'video') return { icon: '▶', tone: 'video' }
    if (outKind === 'audio') return { icon: '♪', tone: 'audio' }
    if (outKind === 'text') return { icon: '✎', tone: 'text' }
    return { icon: '▣', tone: 'image' }
  }

  // kind 看不出来（比如 image_compose）→ 按名字猜
  const name = cap.name || ''
  if (name.includes('视频')) return { icon: '▶', tone: 'video' }
  if (name.includes('音频') || name.includes('配音')) return { icon: '♪', tone: 'audio' }
  if (name.includes('文案') || name.includes('剧本')) return { icon: '✎', tone: 'text' }
  if (name.includes('放大') || name.includes('超分')) return { icon: '⤢', tone: 'image' }
  if (name.includes('图')) return { icon: '▣', tone: 'image' }
  return { icon: '◇', tone: 'generic' }
}

/**
 * 节点卡片的参数摘要 —— 一两行说清「这一步吃什么」。
 *
 * 摘要里不该出现 `input.prompt_a` 这种内部表示 —— 内容同事看到「流程输入 ·
 * 初始画面」才知道这一步在吃什么。
 */
export function paramSummary(
  step: FlowStep,
  cap: NodeCapability | null,
  inputs: TemplateField[] = [],
  stepTitles: Record<string, string> = {},
): string {
  const fields = cap?.fields ?? []
  const parts: string[] = []
  for (const f of fields) {
    const raw = step.params?.[f.name]
    if (raw === undefined || raw === '') continue
    const parsed = parseParamSource(raw)
    const text = describeParam(parsed, inputs, stepTitles)
    parts.push(`${f.label || f.name}：${text}`)
    if (parts.length >= 3) break
  }
  if (!parts.length) return '未设置参数'
  const more = Object.keys(step.params ?? {}).length > parts.length ? ' …' : ''
  return parts.join(' · ') + more
}
