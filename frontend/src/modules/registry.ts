/**
 * 前端模块注册表 — 功能模块插件化
 *
 * 每个功能模块（生成、任务、素材库、管理...）注册到此表，
 * App 按注册表动态渲染 Tab。新增模块只需：
 *
 *   1. 编写组件（接收统一的 ModuleProps）
 *   2. 调用 registerModule({...}) 注册
 *   3. （可选）在 enabled 中配置启停 / roles 控制可见角色
 *
 * 未来扩展（审批流、数据看板等）不改 App 核心代码。
 */
import type { ComponentType } from 'react'
import type { GenerateDraft, Task, Template, User } from '../types'

export interface ModuleProps {
  /** 当前登录用户（模块可据此做细粒度权限） */
  user: User
  /** 可用模板列表 */
  templates: Template[]
  /** 模板变更回调（管理模块修改模板后刷新） */
  onTemplatesChange: () => void
  /** 跨模块导航（如「再生成」从任务页跳到生成页） */
  onNavigate?: (key: string) => void
  /** 「用这些参数再生成」：App 把任务参数转成草稿并切到生成页 */
  onReuseTask?: (task: Task) => void
  /** 待预填的生成草稿（任务页发起，生成页消费后清空） */
  draft?: GenerateDraft | null
  /** 生成页把草稿吃掉后调用（清空 App 里的草稿，防止重复预填） */
  onDraftConsumed?: () => void
}

export interface ModuleDefinition {
  /** 唯一标识 */
  key: string
  /** Tab 显示名 */
  label: string
  /** 渲染组件 */
  component: ComponentType<ModuleProps>
  /** 可见角色，默认所有人 */
  roles?: ('admin' | 'member')[]
  /** 是否启用，默认 true（配置 false 可下线模块） */
  enabled?: boolean
  /** 排序，默认按注册顺序 */
  order?: number
}

// ── 注册表 ──────────────────────────────────

const modules = new Map<string, ModuleDefinition>()

export function registerModule(def: ModuleDefinition): void {
  modules.set(def.key, def)
}

export function unregisterModule(key: string): void {
  modules.delete(key)
}

export function enableModule(key: string, enabled: boolean): void {
  const mod = modules.get(key)
  if (mod) mod.enabled = enabled
}

/** 获取当前用户可见、已启用的模块列表（按 order 排序） */
export function getVisibleModules(user: User): ModuleDefinition[] {
  return [...modules.values()]
    .filter((m) => m.enabled !== false)
    .filter((m) => !m.roles || m.roles.includes(user.role))
    .sort((a, b) => (a.order ?? 99) - (b.order ?? 99))
}

export function getModule(key: string): ModuleDefinition | undefined {
  return modules.get(key)
}
