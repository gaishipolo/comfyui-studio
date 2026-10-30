/** API 客户端 — 带 JWT 认证，统一错误处理 */
import type {
  Template, Task, Asset, User, LoginResponse,
  AssetTag, EngineInfo, WorkflowUploadResponse, WorkflowInventoryResponse,
  UsageResponse, EnginePluginInfo,
} from '../types'

const BASE = '/api'

// ── Token 管理 ─────────────────────────────

export function getToken(): string | null {
  return localStorage.getItem('token')
}

export function setToken(token: string) {
  localStorage.setItem('token', token)
}

export function clearToken() {
  localStorage.removeItem('token')
  localStorage.removeItem('user')
}

export function getStoredUser(): User | null {
  const raw = localStorage.getItem('user')
  return raw ? JSON.parse(raw) : null
}

export function setStoredUser(user: User) {
  localStorage.setItem('user', JSON.stringify(user))
}

// ── 请求封装 ───────────────────────────────

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const token = getToken()
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...(options?.headers as Record<string, string>),
  }
  if (token) headers['Authorization'] = `Bearer ${token}`

  const resp = await fetch(`${BASE}${path}`, { ...options, headers })

  if (resp.status === 401) {
    clearToken()
    window.location.reload()
    throw new Error('登录已过期，请重新登录')
  }
  if (!resp.ok) {
    // 优先提取后端 detail 字段，给出可读错误
    let message = `请求失败 (${resp.status})`
    try {
      const body = await resp.json()
      if (typeof body?.detail === 'string') message = body.detail
    } catch {
      // 非 JSON 响应，保留默认信息
    }
    throw new Error(message)
  }
  return resp.json()
}

// ── 认证 ──────────────────────────────────

export const login = (username: string, password: string) =>
  request<LoginResponse>('/auth/login', {
    method: 'POST',
    body: JSON.stringify({ username, password }),
  })

export const getMe = () => request<User>('/auth/me')

/** 改自己的密码。必须提供当前密码，后端会校验且拒绝新旧相同 */
export const changePassword = (oldPassword: string, newPassword: string) =>
  request<User>('/auth/change-password', {
    method: 'POST',
    body: JSON.stringify({ old_password: oldPassword, new_password: newPassword }),
  })

// ── 账号管理（admin）──────────────────────
// 起因：admin 初始密码是启动时打到控制台的随机串，此前没有任何改密入口，
// 密码等于永远改不掉。改密有了接口之后，还得有人能管理别人的账号。

export const listUsers = () => request<User[]>('/auth/users')

/** 管理员建号（register 接口本身是 admin-only） */
export const registerUser = (data: {
  username: string
  password: string
  display_name?: string
  role?: 'admin' | 'member'
}) =>
  request<User>('/auth/register', {
    method: 'POST',
    body: JSON.stringify(data),
  })

/** 管理员重置他人密码（对方忘记密码时用） */
export const resetUserPassword = (userId: string, newPassword: string) =>
  request<User>(`/auth/users/${userId}/reset-password`, {
    method: 'POST',
    body: JSON.stringify({ new_password: newPassword }),
  })

/** 管理员调整账号：角色 / 启用停用 / 显示名。
 *
 *  后端有两条硬规则：不能把自己停用或降级、系统必须保留至少一个启用的管理员。
 *  违反时是 400 且带可读文案，直接抛给用户看即可。 */
export const updateUser = (
  userId: string,
  data: { role?: 'admin' | 'member'; is_active?: boolean; display_name?: string },
) =>
  request<User>(`/auth/users/${userId}`, { method: 'PATCH', body: JSON.stringify(data) })

// ── 模板 ──────────────────────────────────

export const getTemplates = () => request<Template[]>('/templates')

/** 全量模板（含停用），管理页使用 */
export const getAllTemplates = () => request<Template[]>('/templates?include_inactive=true')

export const getTemplate = (id: string) => request<Template>(`/templates/${id}`)

export const createTemplate = (data: Partial<Template>) =>
  request<Template>('/templates', { method: 'POST', body: JSON.stringify(data) })

export const updateTemplate = (id: string, data: Partial<Template>) =>
  request<Template>(`/templates/${id}`, { method: 'PUT', body: JSON.stringify(data) })

export const deleteTemplate = (id: string) =>
  request<{ message: string }>(`/templates/${id}`, { method: 'DELETE' })

export const uploadWorkflow = async (file: File): Promise<WorkflowUploadResponse> => {
  const form = new FormData()
  form.append('file', file)
  const token = getToken()
  const resp = await fetch(`${BASE}/templates/upload-workflow`, {
    method: 'POST',
    headers: token ? { Authorization: `Bearer ${token}` } : {},
    body: form,
  })
  const data = await resp.json()
  if (!resp.ok) throw new Error(data?.detail || '上传失败')
  return data
}

/** 解析已上传的工作流，拿节点表 / 可注入输入 / 出图节点三份清单 */
export const getWorkflowInventory = (filename: string) =>
  request<WorkflowInventoryResponse>(
    `/templates/workflow-inventory?filename=${encodeURIComponent(filename)}`,
  )

// ── 引擎 ──────────────────────────────────

export const getEngines = () => request<EngineInfo[]>('/engines')

// ── 上传（参考图片）─────────────────────────

export const uploadImage = async (file: File, engine = 'comfyui'): Promise<string> => {
  const form = new FormData()
  form.append('file', file)
  const token = getToken()
  const resp = await fetch(`${BASE}/upload/image?engine=${encodeURIComponent(engine)}`, {
    method: 'POST',
    headers: token ? { Authorization: `Bearer ${token}` } : {},
    body: form,
  })
  const data = await resp.json()
  if (!resp.ok) throw new Error(data?.detail || '上传失败')
  return data.filename as string
}

/** 从素材库选用已有素材推进生成引擎，返回引擎侧文件名（与 uploadImage 同构，
 *  表单拿到文件名后走同一条参数注入链路，无需区分来源） */
export const uploadFromAsset = async (assetId: string, engine = 'comfyui'): Promise<string> => {
  const data = await request<{ filename: string }>('/upload/from-asset', {
    method: 'POST',
    body: JSON.stringify({ asset_id: assetId, engine }),
  })
  return data.filename
}

// ── 任务 ──────────────────────────────────

export const submitTask = (templateId: string, params: Record<string, unknown>, batchCount = 1) =>
  request<Task[]>('/tasks', {
    method: 'POST',
    body: JSON.stringify({ template_id: templateId, params, batch_count: batchCount }),
  })

export const getTask = (id: string) => request<Task>(`/tasks/${id}`)

export const listTasks = (status?: string) =>
  request<Task[]>(`/tasks${status ? `?status=${status}` : ''}`)

export const retryTask = (id: string) =>
  request<Task>(`/tasks/${id}/retry`, { method: 'POST' })

/** 取消任务（排队中或执行中）。引擎侧会一并撤掉/中断，不再占显存 */
export const cancelTask = (id: string) =>
  request<Task>(`/tasks/${id}/cancel`, { method: 'POST' })

export const getTaskAssets = (taskId: string) => request<Asset[]>(`/tasks/${taskId}/assets`)

/** 删除任务（级联删掉它的素材记录与磁盘文件）。仅本人或管理员可删
 *
 *  记录删除**不依赖**文件清理成功，所以 `files_removed` 可能小于 `assets_deleted`，
 *  差额去向见 `file_errors`（如"文件本就不存在"、"SystemExit: ..."）。 */
export const deleteTask = (id: string) =>
  request<{
    message: string
    id: string
    assets_deleted: number
    files_removed: number
    file_errors: string[]
  }>(`/tasks/${id}`, { method: 'DELETE' })

// ── 素材 ──────────────────────────────────

export const listAssets = (params: Record<string, string>) => {
  const qs = new URLSearchParams(params).toString()
  return request<Asset[]>(`/assets${qs ? `?${qs}` : ''}`)
}

export const listAssetTags = () => request<AssetTag[]>('/assets/tags')

export const addAssetTag = (assetId: string, tag: string) =>
  request<{ tags: string[] }>(
    `/assets/${assetId}/tags?tag=${encodeURIComponent(tag)}`,
    { method: 'POST' },
  )

export const removeAssetTag = (assetId: string, tag: string) =>
  request<{ tags: string[] }>(
    `/assets/${assetId}/tags/${encodeURIComponent(tag)}`,
    { method: 'DELETE' },
  )

/** 共享开关：true = 共享给团队（全员可见可下载），false = 收回。
 *  仅本人或管理员可操作 —— 共享的是「看」的权限，别人拿不到编辑/删除权。 */
export const shareAsset = (assetId: string, shared: boolean) =>
  request<Asset>(`/assets/${assetId}/share`, {
    method: 'POST',
    body: JSON.stringify({ shared }),
  })

/** 删除单个素材（记录 + 磁盘文件）。仅本人或管理员可删
 *
 *  `file_removed=false` 不代表失败 —— 记录一定已删，只是磁盘文件没清掉，
 *  原因在 `file_error`。别把它当异常抛给用户。 */
export const deleteAsset = (assetId: string) =>
  request<{ message: string; id: string; file_removed: boolean; file_error: string }>(
    `/assets/${assetId}`,
    { method: 'DELETE' },
  )

export const batchDownload = async (assetIds: string[]): Promise<Blob> => {
  const token = getToken()
  const resp = await fetch(`${BASE}/assets/batch-download`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: JSON.stringify(assetIds),
  })
  if (!resp.ok) throw new Error(`下载失败 (${resp.status})`)
  return resp.blob()
}

// ── 素材文件 URL ───────────────────────────

export const getAssetUrl = (taskId: string, fileName: string) =>
  `${BASE}/tasks/${encodeURIComponent(taskId)}/assets/${encodeURIComponent(fileName)}`

// ⚠️ 素材接口需要 JWT，而 <img src> / <a href> **无法携带 Authorization 头**，
// 直接把 getAssetUrl() 塞进 src 会全部 401（缩略图加载不出来）。
// 因此带鉴权取文件必须走 fetch，再用 object URL 渲染。
//
// 入参约定：必须是 getAssetUrl() 的返回值（已含 BASE，形如 /api/tasks/{id}/assets/{f}）。
// 这里**只做 fetch，不再拼 BASE** —— 否则会拼出 /api/api/tasks/... 直接 404。
export async function fetchAssetBlob(path: string): Promise<Blob> {
  const token = getToken()
  const resp = await fetch(path, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  })
  if (resp.status === 401) {
    clearToken()
    window.location.reload()
    throw new Error('登录已过期，请重新登录')
  }
  if (!resp.ok) throw new Error(`资源加载失败 (${resp.status})`)
  return resp.blob()
}

/** 带鉴权打开素材（新标签），用于替代 <a href> 直链 */
export async function openAssetInNewTab(path: string): Promise<void> {
  const blob = await fetchAssetBlob(path)
  const url = URL.createObjectURL(blob)
  window.open(url, '_blank', 'noopener')
  // 稍后回收，给新标签页留出加载时间
  setTimeout(() => URL.revokeObjectURL(url), 60_000)
}

/** 带鉴权下载素材（替代 <a href download> 直链，直链拿不到 Authorization 头） */
export async function downloadAsset(path: string, fileName: string): Promise<void> {
  const blob = await fetchAssetBlob(path)
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = fileName
  a.click()
  URL.revokeObjectURL(url)
}

// ── 健康检查 ──────────────────────────────

export const getHealth = () => request<{ status: string; comfyui: string }>('/health')

// ── 用量统计（仅 admin）──────────────────────

/** 按时间段统计每人用量（任务数/存储/耗时）。仅管理员，普通用户会拿到 403 */
export const getUsage = (dateFrom?: string, dateTo?: string) => {
  const params = new URLSearchParams()
  if (dateFrom) params.set('date_from', dateFrom)
  if (dateTo) params.set('date_to', dateTo)
  const qs = params.toString()
  return request<UsageResponse>(`/stats/usage${qs ? `?${qs}` : ''}`)
}

// ── 引擎插件（仅 admin）──────────────────────

/** 引擎清单（内置 + 插件）—— 模板配置的引擎下拉 / 能力声明都从这读 */
export const listPlugins = () => request<EnginePluginInfo[]>('/plugins')

/** 上传 Provider 代码（.py）。同名重传 = 升级覆盖；加载失败抛出后端报错详情 */
export const uploadPlugin = async (file: File): Promise<EnginePluginInfo> => {
  const form = new FormData()
  form.append('file', file)
  const token = getToken()
  const resp = await fetch(`${BASE}/plugins/upload`, {
    method: 'POST',
    headers: token ? { Authorization: `Bearer ${token}` } : {},
    body: form,
  })
  const data = await resp.json()
  if (!resp.ok) throw new Error(data?.detail || '上传失败')
  return data as EnginePluginInfo
}

/** 保存引擎运行配置（base_url/api_key 等）—— 存 DB 不进代码，换 key 不用重传 */
export const savePluginConfig = (name: string, config: Record<string, unknown>) =>
  request<{ ok: boolean }>(`/plugins/${encodeURIComponent(name)}/config`, {
    method: 'PUT',
    body: JSON.stringify({ config }),
  })

/** 启停插件（停用后该引擎的模板提交会报引擎不可用） */
export const setPluginEnabled = (name: string, enabled: boolean) =>
  request<{ ok: boolean }>(`/plugins/${encodeURIComponent(name)}/enabled`, {
    method: 'PUT',
    body: JSON.stringify({ enabled }),
  })

/** 删除插件（注销 + 删代码 + 删账，二次确认在界面做） */
export const deletePlugin = (name: string) =>
  request<{ ok: boolean }>(`/plugins/${encodeURIComponent(name)}`, { method: 'DELETE' })

/** 健康检查 —— 真调一次 provider.health_check()，验证配置与连通性 */
export const testPlugin = (name: string) =>
  request<{ ok: boolean; detail: string }>(`/plugins/${encodeURIComponent(name)}/test`, {
    method: 'POST',
  })
