/** 类型定义 — 对齐后端 Schema */

export interface TemplateField {
  name: string
  label: string
  type: 'text' | 'textarea' | 'number' | 'select' | 'image_upload'
  required: boolean
  placeholder?: string
  options?: (string | number | boolean)[]
  default?: string | number | boolean
  advanced?: boolean
}

export interface Template {
  id: string
  name: string
  description: string
  category: string
  icon: string
  input_schema: { fields: TemplateField[] }
  engine: string
  /** 能力 kind（取自引擎 capabilities 声明；空=按分类） */
  capability: string
  workflow_file: string
  param_mapping: Record<string, string>
  output_config: TemplateOutputConfig
  is_active: boolean
  sort_order: number
  created_at: string
  updated_at: string
}

/** 输出配置：只保存勾选的出图节点的结果（nodes 为空会被保存校验拒绝） */
export interface TemplateOutputConfig {
  nodes?: string[]
}

export interface Task {
  id: string
  template_id: string
  created_by?: string
  creator_name?: string  // 创建人显示名（admin 看全部任务时展示）
  batch_id: string
  status: 'pending' | 'running' | 'done' | 'error' | 'cancelled'
  progress: number
  params: Record<string, unknown>
  provider_task_id: string
  result: { outputs?: { filename: string; subfolder: string; type: string; node_id: string }[] }
  error_msg: string
  created_at: string
  started_at: string | null
  finished_at: string | null
}

export interface Asset {
  id: string
  task_id: string
  file_path: string
  file_name: string
  file_size: number
  mime_type: string
  tags: string[]
  meta: Record<string, unknown>
  is_shared?: boolean  // 已共享给团队（共享≠共同所有：别人只能看/下载）
  created_by?: string  // 归属人 id（走 task.created_by）
  creator_name?: string  // 归属人显示名（展示「来自谁」）
  created_at: string
}

export interface User {
  id: string
  username: string
  display_name: string
  role: 'admin' | 'member'
  is_active: boolean
  created_at: string
}

export interface LoginResponse {
  access_token: string
  token_type: string
  user: User
}

export interface AssetTag {
  name: string
  count: number
}

/** 「用这些参数再生成」的草稿：任务页发起，生成页消费后预填表单 */
export interface GenerateDraft {
  template_id: string
  params: Record<string, unknown>
  /** 来源任务（仅用于提示文案） */
  source_task_id?: string
}

export interface EngineInfo {
  name: string
  status: 'connected' | 'disconnected' | 'error'
  detail?: string
}

export interface WorkflowNode {
  id: string
  class_type: string
  title: string
}

/** 一个可注入的工作流输入，path 形如 "76.inputs.value" */
export interface WorkflowInput {
  path: string
  node_id: string
  node_title: string
  class_type: string
  input_name: string
  current_value: string
}

export interface WorkflowOutput {
  node_id: string
  class_type: string
  title: string
}

/** 工作流里界面需要的三份清单：节点表 / 可注入输入 / 出图节点 */
export interface WorkflowInventory {
  nodes: WorkflowNode[]
  inputs: WorkflowInput[]
  outputs: WorkflowOutput[]
}

export interface WorkflowUploadResponse extends WorkflowInventory {
  filename: string
  node_count: number
  message: string
}

export interface WorkflowInventoryResponse extends WorkflowInventory {
  filename: string
}

/** 单个用户的用量统计（一个时间段内）—— 仅 admin 看得到 */
export interface UsageByUser {
  user_id: string
  username: string
  display_name: string
  role: string
  task_total: number
  task_done: number
  task_error: number
  task_cancelled: number
  /** 仅算已完成任务的平均耗时（秒）；没有完成任务为 null */
  avg_duration_seconds: number | null
  storage_bytes: number
  asset_count: number
}

/** 用量统计响应：时间区间回显 + 每人一行 + 合计 */
export interface UsageResponse {
  date_from: string
  date_to: string
  by_user: UsageByUser[]
  totals: UsageByUser
}

// ── 引擎插件（多引擎）─────────────────────────

/** 能力字段声明（与后端 FieldSpec 同构）—— 表单生成与配置表单共用 */
export interface FieldSpec {
  name: string
  label: string
  /** string / int / float / bool / enum / image */
  type: string
  required: boolean
  default: unknown
  /** enum 选项，值直接用 API 原生值 */
  options: string[]
  description: string
}

/** 能力声明（Provider.capabilities 的一项） */
export interface CapabilitySpec {
  kind: string
  label: string
  fields: FieldSpec[]
}

/** 引擎清单条目（内置 + 插件）—— 模板配置的引擎下拉 / 能力声明数据源 */
export interface EnginePluginInfo {
  name: string
  label: string
  capabilities: Record<string, CapabilitySpec>
  config_schema: FieldSpec[]
  builtin: boolean
  enabled: boolean
  /** 最近加载错误（成功为空串） */
  last_error: string
  filename: string
  uploaded_by: string
  config: Record<string, unknown>
  has_config: boolean
}
