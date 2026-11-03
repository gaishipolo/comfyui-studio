/** 类型定义 — 对齐后端 Schema */

export interface TemplateField {
  name: string
  label: string
  /**
   * 控件类型（不是元素类型）。
   * image_upload 是历史名字，后端读取时已统一成 file_upload + accept=image，
   * 这里保留兼容，前端渲染两者走同一分支
   *
   * random_number = 数字输入 + 「随机」按钮，挂载时自动取一个随机整数
   * （典型用途：seed）。值仍是普通数字，提交时按数字下发；填 -1 表示
   * 「每次生成自动换一个」，由后端在提交时展开并回写真实值
   */
  type: 'text' | 'textarea' | 'number' | 'select' | 'file_upload' | 'image_upload' | 'random_number'
  required: boolean
  placeholder?: string
  options?: (string | number | boolean)[]
  default?: string | number | boolean
  advanced?: boolean
  /** 上传控件放行的文件种类（仅 file_upload 有意义），缺省 image */
  accept?: 'image' | 'audio' | 'video'
  /** 基数：true = 表单值是数组。**与控件类型正交**，任何类型都能是列表 */
  multiple?: boolean
  /** 数值下界（number / random_number）。缺省 0 */
  min?: number
  /** 数值上界（number / random_number）。缺省 2147483647 */
  max?: number
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

/** 导入模板的回执：warnings 是「导进来了，但你可能想知道」的事
 *  （本机没装那个引擎、工作流没跟着文件走、重名被改了…） */
export interface TemplateImportResult {
  template: Template
  warnings: string[]
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
  // node_id 只有 ComfyUI 系引擎有（插件 provider 的产物没有节点概念），前端只用 length，故为可选
  result: { outputs?: { filename: string; subfolder: string; type: string; node_id?: string }[] }
  error_msg: string
  created_at: string
  started_at: string | null
  finished_at: string | null
}

export interface Asset {
  id: string
  /** 来源任务：生成素材指向产出它的任务，用户上传的素材为 null */
  task_id: string | null
  /** FileStore 相对键（不是服务器绝对磁盘路径）；保留给诊断/验证。 */
  file_path: string
  file_name: string
  file_size: number
  mime_type: string
  tags: string[]
  meta: Record<string, unknown>
  is_shared?: boolean  // 已共享给团队（共享≠共同所有：别人只能看/下载）
  created_by?: string  // 归属人 id（assets.owner_id）
  creator_name?: string  // 归属人显示名（展示「来自谁」）
  created_at: string
  /** 进回收站的时间；null / 缺省 = 正常素材。仅回收站接口会返回非空值 */
  deleted_at?: string | null
}

/** 存储配额概览。`used_bytes` **含回收站** —— 软删除不动磁盘文件。
 *  `quota_bytes=0` + `unlimited=true` 表示不限；此时 `percent` 为 null。 */
export interface StorageQuota {
  used_bytes: number
  quota_bytes: number
  unlimited: boolean
  percent: number | null
}

export interface User {
  id: string
  username: string
  display_name: string
  role: 'admin' | 'member'
  is_active: boolean
  /** 存储配额（字节），0 = 不限。仅对 member 生效，admin 豁免准入检查 */
  storage_quota_bytes?: number
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
  /** unknown = 该引擎没实现 health_check（「不知道」，不等于「离线」） */
  status: 'connected' | 'disconnected' | 'error' | 'unknown'
  /** 异常详情，只有 admin 拿得到 */
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
  options: (string | number | boolean)[]
  description: string
  /** 多值字段（图片可一次上传多张，表单值变数组） */
  multiple: boolean
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
