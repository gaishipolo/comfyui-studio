/** 引擎插件管理（仅 admin）— 上传 Provider 代码，配置、启停、测试
 *
 *  多引擎的核心入口：管理员（=开发）按 docs/provider-dev-guide.md 写一个
 *  Provider 类文件，在这里上传即注册生效 —— 不改仓库、不发版。
 *
 *  安全提示常驻页面：上传 = 服务端执行任意代码，只给 admin 且留审计。
 *  机密（api_key 等）填在「配置」里存 DB，不进代码文件，换 key 不用重传。 */
import { useCallback, useEffect, useRef, useState } from 'react'
import type { EnginePluginInfo, FieldSpec } from '../../types'
import {
  listPlugins, uploadPlugin, savePluginConfig, setPluginEnabled, deletePlugin, testPlugin,
} from '../../api/client'
import type { ModuleProps } from '../../modules/registry'

/** 配置项名含 key/secret/token 的用密码框，别明文晃 */
const isSecretField = (name: string) => /key|secret|token|password/i.test(name)

export default function PluginManager({ user }: ModuleProps) {
  const [plugins, setPlugins] = useState<EnginePluginInfo[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [uploading, setUploading] = useState(false)
  // 配置编辑态：engine name -> {字段: 值}；editingName 控制展开哪个
  const [editingName, setEditingName] = useState('')
  const [configDraft, setConfigDraft] = useState<Record<string, string>>({})
  const [testResult, setTestResult] = useState<Record<string, string>>({})
  const fileInputRef = useRef<HTMLInputElement>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      setPlugins(await listPlugins())
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  if (user.role !== 'admin') {
    return <p className="py-xl text-center text-base text-muted">仅管理员可管理引擎插件</p>
  }

  const handleUpload = async (file: File) => {
    setUploading(true)
    setNotice('')
    setError('')
    try {
      const info = await uploadPlugin(file)
      setNotice(`上传成功：${info.label}（${info.name}）已注册。记得在「配置」里填 base_url / api_key。`)
      await load()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setUploading(false)
      if (fileInputRef.current) fileInputRef.current.value = ''
    }
  }

  const startEditConfig = (p: EnginePluginInfo) => {
    setEditingName(p.name)
    const draft: Record<string, string> = {}
    for (const f of p.config_schema) {
      const v = p.config[f.name]
      draft[f.name] = v === undefined || v === null ? (f.default != null ? String(f.default) : '') : String(v)
    }
    setConfigDraft(draft)
  }

  const handleSaveConfig = async (name: string, schema: FieldSpec[]) => {
    // 草稿是字符串，按声明类型转回去（int/float/bool），免得类型漂移
    const config: Record<string, unknown> = {}
    for (const f of schema) {
      const raw = configDraft[f.name]
      if (raw === undefined) continue
      if (raw === '' && !f.required) continue
      if (f.type === 'int') config[f.name] = parseInt(raw, 10) || 0
      else if (f.type === 'float') config[f.name] = parseFloat(raw) || 0
      else if (f.type === 'bool') config[f.name] = raw === 'true' || raw === '1'
      else config[f.name] = raw
    }
    setError('')
    try {
      await savePluginConfig(name, config)
      setNotice(`「${name}」配置已保存`)
      setEditingName('')
      await load()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    }
  }

  const handleToggle = async (p: EnginePluginInfo) => {
    setError('')
    try {
      await setPluginEnabled(p.name, !p.enabled)
      await load()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    }
  }

  const handleTest = async (name: string) => {
    setTestResult((m) => ({ ...m, [name]: '测试中…' }))
    try {
      const r = await testPlugin(name)
      setTestResult((m) => ({ ...m, [name]: r.ok ? '✓ 连通正常' : `✗ ${r.detail || '未通过'}` }))
    } catch (err) {
      setTestResult((m) => ({ ...m, [name]: `✗ ${err instanceof Error ? err.message : String(err)}` }))
    }
  }

  const handleDelete = async (p: EnginePluginInfo) => {
    if (!window.confirm(`删除引擎「${p.label}」？代码文件会被删除，引用它的模板将无法提交。`)) return
    setError('')
    try {
      await deletePlugin(p.name)
      await load()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    }
  }

  return (
    <section>
      <div className="flex flex-wrap items-center justify-between gap-md">
        <div>
          <h2 className="font-display text-section">引擎插件</h2>
          <p className="text-sm text-muted">
            上传 Provider 代码接入新供应商。开发规范见 <code>docs/provider-dev-guide.md</code>，
            示例见 <code>backend/examples/provider_template.py</code>
          </p>
        </div>
        <div className="flex items-center gap-xs">
          <input
            ref={fileInputRef}
            type="file"
            accept=".py"
            className="hidden"
            onChange={(e) => {
              const f = e.target.files?.[0]
              if (f) handleUpload(f)
            }}
          />
          <button className="btn-primary" disabled={uploading} onClick={() => fileInputRef.current?.click()}>
            {uploading ? '上传中…' : '上传插件 (.py)'}
          </button>
        </div>
      </div>

      <p className="mt-sm rounded-md bg-warning/10 px-md py-sm text-sm text-warning">
        上传的代码会在服务端执行，拥有系统全部权限 —— 仅限可信的管理员上传。密钥请填在「配置」里，不要写进代码。
      </p>

      {notice && <p className="mt-sm text-sm text-success">{notice}</p>}
      {error && <p className="mt-sm text-sm text-danger">{error}</p>}

      <div className="mt-lg flex flex-col gap-md">
        {loading && <p className="text-sm text-muted">加载中…</p>}
        {!loading && plugins.length === 0 && (
          <p className="py-xl text-center text-base text-muted">还没有引擎</p>
        )}
        {plugins.map((p) => (
          <article key={p.name} className="rounded-lg border border-tertiary p-md">
            <div className="flex flex-wrap items-center justify-between gap-sm">
              <div>
                <div className="flex items-center gap-sm">
                  <h3 className="font-display text-base">{p.label}</h3>
                  <code className="text-sm text-muted">{p.name}</code>
                  {p.builtin && <span className="text-xs text-muted">内置</span>}
                  {!p.enabled && <span className="text-xs text-warning">已停用</span>}
                </div>
                <div className="mt-xs flex flex-wrap gap-xs">
                  {Object.values(p.capabilities).map((c) => (
                    <span key={c.kind} className="rounded bg-secondary px-sm py-xs text-xs text-secondary">
                      {c.label}
                    </span>
                  ))}
                  {Object.values(p.capabilities).length === 0 && (
                    <span className="text-xs text-muted">（未加载 — 见下方报错）</span>
                  )}
                </div>
              </div>
              <div className="flex flex-wrap items-center gap-xs">
                {!p.builtin && p.config_schema.length > 0 && (
                  <button className="btn-quiet" onClick={() => startEditConfig(p)}>配置</button>
                )}
                <button className="btn-quiet" onClick={() => handleTest(p.name)}>测试</button>
                {!p.builtin && (
                  <>
                    <button className="btn-quiet" onClick={() => handleToggle(p)}>
                      {p.enabled ? '停用' : '启用'}
                    </button>
                    <button className="btn-danger-quiet" onClick={() => handleDelete(p)}>删除</button>
                  </>
                )}
              </div>
            </div>

            {p.last_error && (
              <p className="mt-sm rounded bg-danger/10 px-sm py-xs text-sm text-danger">
                加载错误：{p.last_error}
              </p>
            )}
            {testResult[p.name] && (
              <p className={`mt-sm text-sm ${testResult[p.name].startsWith('✓') ? 'text-success' : 'text-danger'}`}>
                {testResult[p.name]}
              </p>
            )}

            {editingName === p.name && (
              <div className="mt-md rounded-md bg-secondary p-md">
                <h4 className="text-sm font-display">运行配置（存数据库，不进代码）</h4>
                <div className="mt-sm flex flex-col gap-sm">
                  {p.config_schema.map((f) => (
                    <label key={f.name} className="flex items-center gap-sm">
                      <span className="w-40 text-sm text-secondary">
                        {f.label}
                        {f.required && <span className="text-danger"> *</span>}
                      </span>
                      {f.type === 'enum' && f.options.length > 0 ? (
                        <select
                          className="field w-auto"
                          value={configDraft[f.name] ?? ''}
                          onChange={(e) => setConfigDraft((d) => ({ ...d, [f.name]: e.target.value }))}
                        >
                          <option value="">请选择</option>
                          {f.options.map((o) => (
                            <option key={o} value={o}>{o}</option>
                          ))}
                        </select>
                      ) : (
                        <input
                          className="field w-auto"
                          type={isSecretField(f.name) ? 'password' : f.type === 'int' || f.type === 'float' ? 'number' : 'text'}
                          placeholder={f.description || f.name}
                          value={configDraft[f.name] ?? ''}
                          onChange={(e) => setConfigDraft((d) => ({ ...d, [f.name]: e.target.value }))}
                        />
                      )}
                    </label>
                  ))}
                </div>
                <div className="mt-md flex gap-xs">
                  <button className="btn-primary" onClick={() => handleSaveConfig(p.name, p.config_schema)}>
                    保存配置
                  </button>
                  <button className="btn-quiet" onClick={() => setEditingName('')}>取消</button>
                </div>
              </div>
            )}
          </article>
        ))}
      </div>
    </section>
  )
}
