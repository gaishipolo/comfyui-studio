/** API 客户端 — 带 JWT 认证，统一错误处理 */
import type {
  Template, Task, Asset, User, LoginResponse,
  AssetTag, EngineInfo, WorkflowUploadResponse, WorkflowInventoryResponse,
  UsageResponse, EnginePluginInfo, TemplateImportResult, StorageQuota,
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
  if (!raw) return null
  try {
    return JSON.parse(raw) as User
  } catch {
    localStorage.removeItem('user')
    return null
  }
}

export function setStoredUser(user: User) {
  localStorage.setItem('user', JSON.stringify(user))
}

// ── 请求封装 ───────────────────────────────

async function authFetch(
  input: RequestInfo | URL,
  options?: RequestInit,
  reloadOn401 = true,
): Promise<Response> {
  const headers = new Headers(options?.headers)
  const token = getToken()
  if (token && !headers.has('Authorization')) headers.set('Authorization', `Bearer ${token}`)
  const resp = await fetch(input, { ...options, headers })
  if (resp.status === 401 && reloadOn401) {
    clearToken()
    window.location.reload()
    throw new Error('登录已过期，请重新登录')
  }
  return resp
}

/** 从失败响应里提取可读错误：优先后端 `detail`，其次纯文本，最后才用状态码兜底。
 *
 *  为什么不能直接 `resp.json()`：413（反代 body 上限）、502/504 返回的是 HTML
 *  错误页，`json()` 会先抛 `SyntaxError: Unexpected token '<'`，把真正的状态码
 *  彻底盖掉，用户看到的是"上传失败：Unexpected token '<'"。 */
export async function responseErrorMessage(
  resp: Response,
  fallback: string,
): Promise<string> {
  // body 只能读一次：真 Response 有 text()，先整段读出来再试 JSON.parse，
  // 这样非 JSON（反代返回的 HTML 错误页）还能退回文本。
  // 单测里的替身有时只实现 json()，那种情况走下面的兼容分支。
  let raw: string | null = null
  if (typeof resp.text === 'function') {
    try {
      raw = await resp.text()
    } catch {
      return `${fallback} (${resp.status})`
    }
  }

  if (raw !== null) {
    try {
      const body = JSON.parse(raw)
      if (typeof body?.detail === 'string' && body.detail) return body.detail
    } catch {
      // 不是 JSON，落到下面的文本处理
    }
    const text = raw.trim()
    // HTML 错误页对用户毫无意义，不如只报状态码
    if (text && !text.startsWith('<')) return text.slice(0, 200)
    return `${fallback} (${resp.status})`
  }

  try {
    const body = await resp.json()
    if (typeof body?.detail === 'string' && body.detail) return body.detail
  } catch {
    // 读不出来就退回状态码
  }
  return `${fallback} (${resp.status})`
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const headers = new Headers(options?.headers)
  if (!headers.has('Content-Type')) headers.set('Content-Type', 'application/json')
  // 登录 401 是「凭据错误」，不是「现有会话过期」；reload 会把后端错误吞掉。
  const resp = await authFetch(
    `${BASE}${path}`,
    { ...options, headers },
    path !== '/auth/login',
  )
  if (!resp.ok) {
    throw new Error(await responseErrorMessage(resp, '请求失败'))
  }
  return resp.json()
}

// ── 认证 ──────────────────────────────────

export const login = (username: string, password: string) =>
  request<LoginResponse>('/auth/login', {
    method: 'POST',
    body: JSON.stringify({ username, password }),
  })

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

/** 管理员调整账号：角色 / 启用停用 / 显示名 / 存储配额。
 *
 *  后端有两条硬规则：不能把自己停用或降级、系统必须保留至少一个启用的管理员。
 *  违反时是 400 且带可读文案，直接抛给用户看即可。
 *
 *  `storage_quota_bytes` 传 0 = 不限。只对 member 生效（admin 豁免准入检查）。 */
export const updateUser = (
  userId: string,
  data: {
    role?: 'admin' | 'member'
    is_active?: boolean
    display_name?: string
    storage_quota_bytes?: number
  },
) =>
  request<User>(`/auth/users/${userId}`, { method: 'PATCH', body: JSON.stringify(data) })

// ── 模板 ──────────────────────────────────

export const getTemplates = () => request<Template[]>('/templates')

/** 全量模板（含停用），管理页使用 */
export const getAllTemplates = () => request<Template[]>('/templates?include_inactive=true')

export const createTemplate = (data: Partial<Template>) =>
  request<Template>('/templates', { method: 'POST', body: JSON.stringify(data) })

export const updateTemplate = (id: string, data: Partial<Template>) =>
  request<Template>(`/templates/${id}`, { method: 'PUT', body: JSON.stringify(data) })

export const deleteTemplate = (id: string) =>
  request<{ message: string }>(`/templates/${id}`, { method: 'DELETE' })

export const uploadWorkflow = async (file: File): Promise<WorkflowUploadResponse> => {
  const form = new FormData()
  form.append('file', file)
  const resp = await authFetch(`${BASE}/templates/upload-workflow`, {
    method: 'POST',
    body: form,
  })
  if (!resp.ok) throw new Error(await responseErrorMessage(resp, '上传失败'))
  return resp.json()
}

/** 解析已上传的工作流，拿节点表 / 可注入输入 / 出图节点三份清单 */
export const getWorkflowInventory = (filename: string) =>
  request<WorkflowInventoryResponse>(
    `/templates/workflow-inventory?filename=${encodeURIComponent(filename)}`,
  )

// ── 模板导出 / 导入 ────────────────────────

/** 下载模板导出文件（配置 + 内嵌工作流）。
 *  走 fetch 而不是 <a href>：导出接口要 JWT，直链发不出 Authorization 头 */
export async function exportTemplate(templateId: string, templateName: string): Promise<void> {
  const blob = await fetchAssetBlob(`${BASE}/templates/${templateId}/export`, { silent: false })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `${templateName}.template.json`
  document.body.appendChild(a)
  a.click()
  a.remove()
  // 不能紧接着 revoke：下载还没真正开始读 blob，Safari/Firefox 可能存出 0 字节
  setTimeout(() => URL.revokeObjectURL(url), 60_000)
}

/** 导入模板导出文件。overwrite=true 时同名模板被覆盖（保留原 id，历史任务不断链） */
export const importTemplate = (payload: unknown, overwrite = false) =>
  request<TemplateImportResult>(`/templates/import?overwrite=${overwrite}`, {
    method: 'POST',
    body: JSON.stringify(payload),
  })

// ── 引擎 ──────────────────────────────────

export const getEngines = () => request<EngineInfo[]>('/engines')

// ── 上传（参考图片）─────────────────────────

/** 上传参考文件（图片 / 音频 / 视频），返回引擎侧文件名。
 *  kind 决定后端放行哪类扩展名与大小上限 —— 别在这里按类型分支，统一一个入口 */
export const uploadInput = async (
  file: File,
  engine = 'comfyui',
  kind: 'image' | 'audio' | 'video' = 'image',
): Promise<string> => {
  const form = new FormData()
  form.append('file', file)
  const resp = await authFetch(
    `${BASE}/upload/file?engine=${encodeURIComponent(engine)}&kind=${kind}`,
    {
      method: 'POST',
      body: form,
    },
  )
  if (!resp.ok) throw new Error(await responseErrorMessage(resp, '上传失败'))
  const data = await resp.json()
  return data.filename as string
}

/** 从素材库选用已有素材推进生成引擎，返回引擎侧文件名（与 uploadInput 同构，
 *  表单拿到文件名后走同一条参数注入链路，无需区分来源） */
export const uploadFromAsset = async (assetId: string, engine = 'comfyui'): Promise<string> => {
  const data = await request<{ filename: string }>('/upload/from-asset', {
    method: 'POST',
    body: JSON.stringify({ asset_id: assetId, engine }),
  })
  return data.filename
}

/** 把文件直接存进素材库（不经过引擎）。
 *
 *  与 uploadInput 的区别：那个是「给这次生成提供参考文件」，传完即丢；
 *  这里落的是素材库里的正式条目，之后能在「从素材库选择」里复用。
 *  多文件一次提交，后端整体成功或整体拒绝。
 *
 *  用 XHR 而不是 fetch：视频能到 200MB，fetch 拿不到上传进度，用户只能干等。
 */
export const uploadAssets = (
  files: File[],
  onProgress?: (percent: number) => void,
): Promise<Asset[]> =>
  new Promise((resolve, reject) => {
    const form = new FormData()
    for (const file of files) form.append('files', file)

    const xhr = new XMLHttpRequest()
    xhr.open('POST', `${BASE}/assets/upload`)
    const token = getToken()
    if (token) xhr.setRequestHeader('Authorization', `Bearer ${token}`)

    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable && onProgress) {
        onProgress(Math.round((event.loaded / event.total) * 100))
      }
    }
    xhr.onload = () => {
      let payload: unknown = null
      try {
        payload = JSON.parse(xhr.responseText)
      } catch {
        // 非 JSON（网关错误页等）→ 走下面的状态码分支
      }
      if (xhr.status === 401) {
        clearToken()
        window.location.reload()
        reject(new Error('登录已过期，请重新登录'))
        return
      }
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(payload as Asset[])
        return
      }
      const detail = (payload as { detail?: string } | null)?.detail
      reject(new Error(detail || `上传失败（HTTP ${xhr.status}）`))
    }
    xhr.onerror = () => reject(new Error('网络中断，上传失败'))
    xhr.onabort = () => reject(new Error('上传已取消'))
    xhr.send(form)
  })

// ── 任务 ──────────────────────────────────

export const submitTask = (templateId: string, params: Record<string, unknown>, batchCount = 1) =>
  request<Task[]>('/tasks', {
    method: 'POST',
    body: JSON.stringify({ template_id: templateId, params, batch_count: batchCount }),
  })

/** 任务列表。`limit` 显式传：后端默认只给 50 条，不传就等于静默截断，
 *  超过 50 条后旧任务会凭空消失（且没有翻页入口）。 */
export const listTasks = (
  status?: string,
  opts: { limit?: number; offset?: number; batchId?: string } = {},
) => {
  const qs = new URLSearchParams()
  if (status) qs.set('status', status)
  if (opts.batchId) qs.set('batch_id', opts.batchId)
  if (opts.limit != null) qs.set('limit', String(opts.limit))
  if (opts.offset) qs.set('offset', String(opts.offset))
  const suffix = qs.toString()
  return request<Task[]>(`/tasks${suffix ? `?${suffix}` : ''}`)
}

export const retryTask = (id: string) =>
  request<Task>(`/tasks/${id}/retry`, { method: 'POST' })

/** 取消任务（排队中或执行中）。引擎侧会一并撤掉/中断，不再占显存 */
export const cancelTask = (id: string) =>
  request<Task>(`/tasks/${id}/cancel`, { method: 'POST' })

export const getTaskAssets = (taskId: string) => request<Asset[]>(`/tasks/${taskId}/assets`)

/** 单个任务。会话面板按 id 补拉用 —— 较早的任务会掉出最近一页列表 */
export const getTask = (id: string) => request<Task>(`/tasks/${id}`)

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

/** 删除单个素材 → **移入回收站**（软删除）。仅本人或管理员可删
 *
 *  后端只打 `deleted_at` 时间戳，**不动磁盘文件**：误删能恢复，但空间不会
 *  因此释放。真要腾空间走 `purgeAsset`。 */
export const deleteAsset = (assetId: string) =>
  request<{ message: string; id: string; deleted_at: string }>(
    `/assets/${assetId}`,
    { method: 'DELETE' },
  )

// ── 回收站 ─────────────────────────────────

/** 回收站列表（只看自己删的；管理员看全部） */
export const listTrash = (limit = 50, offset = 0) =>
  request<Asset[]>(`/assets/trash?limit=${limit}&offset=${offset}`)

/** 从回收站恢复 */
export const restoreAsset = (assetId: string) =>
  request<Asset>(`/assets/${assetId}/restore`, { method: 'POST' })

/** 彻底删除（记录 + 磁盘文件，不可恢复）。只对回收站里的素材生效 */
export const purgeAsset = (assetId: string) =>
  request<{
    message: string
    id: string
    file_removed: boolean
    file_error: string
    thumbnail_removed: boolean
    thumbnail_error: string
  }>(`/assets/${assetId}/purge`, { method: 'DELETE' })

/** 清空回收站。分批执行，`remaining>0` 表示还没清完，需要再来一次 */
export const emptyTrash = () =>
  request<{
    deleted: number
    files_removed: number
    file_errors: string[]
    remaining: number
  }>('/assets/trash', { method: 'DELETE' })

// ── 存储配额 ───────────────────────────────

/** 当前用户的配额概览。`used_bytes` 含回收站（文件还在盘上） */
export const getQuota = () => request<StorageQuota>('/assets/quota')

/** 准备浏览器原生批量下载：返回一次性下载地址（ZIP 由下载管理器流式保存，不进页面内存） */
export const prepareBatchDownload = async (assetIds: string[]): Promise<string> => {
  const resp = await authFetch(`${BASE}/assets/batch-download/prepare`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify(assetIds),
  })
  if (!resp.ok) {
    let message = `下载准备失败 (${resp.status})`
    try {
      const body = await resp.json()
      if (typeof body?.detail === 'string') message = body.detail
    } catch {
      // 非 JSON 响应，保留默认信息
    }
    throw new Error(message)
  }
  const body = (await resp.json()) as { url?: string }
  if (typeof body?.url !== 'string') {
    throw new Error('下载准备未返回有效地址')
  }
  return body.url
}

// ── 素材文件 URL ───────────────────────────

/** 素材文件 URL。按 **asset_id** 定位 —— 素材可以没有来源任务（用户上传）。 */
export const getAssetUrl = (assetId: string) =>
  `${BASE}/assets/${encodeURIComponent(assetId)}/file`

/** 缩略图 URL（图片首访懒生成 / 视频落盘时抽帧）。生成失败时后端回退原图 */
export const getAssetThumbUrl = (assetId: string) =>
  `${BASE}/assets/${encodeURIComponent(assetId)}/thumb`

// ⚠️ 素材接口需要 JWT，而 <img src> / <a href> **无法携带 Authorization 头**，
// 直接把 getAssetUrl() 塞进 src 会全部 401（缩略图加载不出来）。
// 因此带鉴权取文件必须走 fetch，再用 object URL 渲染。
//
// 入参约定：必须是 getAssetUrl() 的返回值（已含 BASE，形如 /api/assets/{id}/file）。
// 这里**只做 fetch，不再拼 BASE** —— 否则会拼出 /api/api/assets/... 直接 404。
export class AssetFetchError extends Error {
  readonly status: number

  constructor(status: number, message: string) {
    super(message)
    this.name = 'AssetFetchError'
    this.status = status
  }
}

export interface AssetFetchOptions {
  silent?: boolean
  signal?: AbortSignal
}

export function fetchAssetBlob(path: string): Promise<Blob>
export function fetchAssetBlob(
  path: string,
  options: { silent?: false; signal?: AbortSignal },
): Promise<Blob>
export function fetchAssetBlob(
  path: string,
  options: { silent: true; signal?: AbortSignal },
): Promise<Blob | null>
export function fetchAssetBlob(path: string, options: AssetFetchOptions): Promise<Blob | null>
/** silent=true preserves the legacy nullable call shape; 401 and abort still propagate. */
export async function fetchAssetBlob(
  path: string,
  options?: AssetFetchOptions,
): Promise<Blob | null> {
  const token = getToken()
  let resp: Response
  try {
    resp = await fetch(path, {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
      signal: options?.signal,
    })
  } catch (error) {
    if (options?.signal?.aborted || (error instanceof DOMException && error.name === 'AbortError')) {
      throw error
    }
    const fetchError = new AssetFetchError(0, '资源加载失败（网络错误）')
    if (options?.silent) return null
    throw fetchError
  }
  if (resp.status === 401) {
    clearToken()
    window.location.reload()
    throw new AssetFetchError(401, '登录已过期，请重新登录')
  }
  if (!resp.ok) {
    let message = `资源加载失败 (${resp.status})`
    try {
      const body = await resp.json()
      if (typeof body?.detail === 'string') message = body.detail
    } catch {
      // 非 JSON 响应，保留默认信息
    }
    if (options?.silent) return null
    throw new AssetFetchError(resp.status, message)
  }
  try {
    return await resp.blob()
  } catch (error) {
    if (options?.signal?.aborted || (error instanceof DOMException && error.name === 'AbortError')) {
      throw error
    }
    if (options?.silent) return null
    throw new AssetFetchError(0, '资源加载失败（网络错误）')
  }
}

/** 带鉴权打开素材（新标签），用于替代 <a href> 直链 */
export async function openAssetInNewTab(path: string): Promise<void> {
  // 必须在用户点击的同步调用栈里先开标签页；等 fetch 后再 window.open 会被
  // 浏览器当成异步弹窗拦截。先开空白页，鉴权取流完成后再导航到 Blob URL。
  const popup = window.open('about:blank', '_blank')
  if (!popup) throw new Error('浏览器拦截了新标签页，请允许本站弹出窗口')
  popup.opener = null
  try {
    const blob = await fetchAssetBlob(path, { silent: false })
    const url = URL.createObjectURL(blob)
    popup.location.href = url
    // 稍后回收，给新标签页留出加载时间
    setTimeout(() => URL.revokeObjectURL(url), 60_000)
  } catch (error) {
    popup.close()
    throw error
  }
}

/** 带鉴权下载素材（替代 <a href download> 直链，直链拿不到 Authorization 头） */
export async function downloadAsset(path: string, fileName: string): Promise<void> {
  const blob = await fetchAssetBlob(path, { silent: false })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = fileName
  document.body.appendChild(a)
  a.click()
  a.remove()
  // 同 exportTemplate：立即 revoke 会让部分浏览器存出空文件
  setTimeout(() => URL.revokeObjectURL(url), 60_000)
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
  const resp = await authFetch(`${BASE}/plugins/upload`, {
    method: 'POST',
    body: form,
  })
  if (!resp.ok) throw new Error(await responseErrorMessage(resp, '上传失败'))
  const data = await resp.json()
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
