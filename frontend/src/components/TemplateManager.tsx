/** 模板管理 — admin 专属：新建/编辑/启停模板 + 工作流上传 + 参数映射配置 */
import { useState, useEffect, useCallback, useMemo } from 'react'
import type {
  Template, TemplateField, EnginePluginInfo, FieldSpec, TemplateOutputConfig,
  WorkflowInventory, WorkflowInput,
} from '../types'
import {
  getAllTemplates, createTemplate, updateTemplate, deleteTemplate,
  uploadWorkflow, getWorkflowInventory, listPlugins,
} from '../api/client'

interface Props {
  onTemplatesChange: () => void
}

interface TemplateFormData {
  name: string
  description: string
  category: string
  icon: string
  engine: string
  capability: string
  workflow_file: string
  input_schema: { fields: TemplateField[] }
  param_mapping: Record<string, string>
  output_config: TemplateOutputConfig
  is_active: boolean
  sort_order: number
}

const emptyForm: TemplateFormData = {
  name: '',
  description: '',
  category: 'image',
  icon: '',
  engine: 'comfyui',
  capability: '',
  workflow_file: '',
  input_schema: {
    fields: [
      { name: 'prompt', label: '提示词', type: 'textarea', required: true },
      { name: 'width', label: '宽度', type: 'select', required: false, options: [512, 768, 1024, 1344], default: 1024 },
      { name: 'height', label: '高度', type: 'select', required: false, options: [512, 768, 1024, 1344], default: 1024 },
    ],
  },
  param_mapping: {},
  output_config: { nodes: [] },
  is_active: true,
  sort_order: 0,
}

const categoryLabel: Record<string, string> = {
  image: '图像',
  video: '视频',
  text: '文本',
}

export default function TemplateManager({ onTemplatesChange }: Props) {
  const [templates, setTemplates] = useState<Template[]>([])
  const [plugins, setPlugins] = useState<EnginePluginInfo[]>([])
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState('')
  const [editing, setEditing] = useState<TemplateFormData | null>(null)
  const [editId, setEditId] = useState<string | null>(null)
  const [uploadStatus, setUploadStatus] = useState('')
  const [uploadTone, setUploadTone] = useState<'accent' | 'error'>('accent')
  const [saving, setSaving] = useState(false)
  // 当前工作流的三份清单（节点表 / 可注入输入 / 出图节点），映射下拉与出图勾选都靠它
  const [inventory, setInventory] = useState<WorkflowInventory | null>(null)
  const [inventoryLoading, setInventoryLoading] = useState(false)
  const [inventoryError, setInventoryError] = useState('')

  const fetchTemplates = useCallback(async () => {
    setLoading(true)
    setLoadError('')
    try {
      // 管理页需要看到已停用模板（可重新启用）
      setTemplates(await getAllTemplates())
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : '模板列表加载失败')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchTemplates()
    listPlugins().then(setPlugins).catch(() => setPlugins([]))
  }, [fetchTemplates])

  // ── 工作流清单 ──────────────────────────

  const loadInventory = useCallback(async (filename: string) => {
    if (!filename) {
      setInventory(null)
      setInventoryError('')
      return
    }
    setInventoryLoading(true)
    setInventoryError('')
    try {
      setInventory(await getWorkflowInventory(filename))
    } catch (err) {
      setInventory(null)
      const msg = err instanceof Error ? err.message : '工作流解析失败'
      // 这个接口回「Template not found」只有一种解释：请求落进了 GET /{template_id}，
      // 即后端进程跑的是加路由之前的旧代码（前端 dist 即时生效、后端要重启，会偏斜）
      setInventoryError(
        msg === 'Template not found'
          ? '接口返回 404 —— 后端服务多半在跑旧代码，重启后端后再打开这个模板'
          : msg,
      )
    } finally {
      setInventoryLoading(false)
    }
  }, [])

  // ── 工作流上传 ──────────────────────────

  const handleWorkflowUpload = async (file: File) => {
    setUploadTone('accent')
    setUploadStatus('上传中…')
    try {
      const data = await uploadWorkflow(file)
      // 旧后端的回执里没有清单字段（前端 build 即时生效，代码偏斜就在这暴露）
      if (!Array.isArray(data.outputs) || !Array.isArray(data.inputs)) {
        setUploadTone('error')
        setUploadStatus('后端返回缺少工作流清单字段 —— 后端服务在跑旧代码，重启后端后再传一次')
        return
      }
      // 换了工作流文件 = 节点图变了，旧映射与旧勾选全部作废，直接清空
      setInventory({ nodes: data.nodes, inputs: data.inputs, outputs: data.outputs })
      setInventoryError('')
      setUploadTone(data.outputs.length > 0 ? 'accent' : 'error')
      setUploadStatus(data.message)
      if (editing) {
        setEditing({
          ...editing,
          workflow_file: data.filename,
          param_mapping: {},
          output_config: { nodes: [] },
        })
      }
    } catch (err) {
      setUploadTone('error')
      setUploadStatus(err instanceof Error ? err.message : '上传失败')
    }
  }

  // ── 保存模板 ────────────────────────────

  const handleSave = async () => {
    if (!editing) return
    if (!editing.name.trim()) return alert('请填写模板名称')
    // 配了工作流就必须显式勾选出图节点 —— 后端会再校验一遍，这里先给即时反馈
    if (editing.workflow_file && (editing.output_config.nodes ?? []).length === 0) {
      return alert('请至少勾选一个出图节点（决定任务保存哪些节点的结果）')
    }
    setSaving(true)
    try {
      if (editId) {
        await updateTemplate(editId, editing as Partial<Template>)
      } else {
        await createTemplate(editing as Partial<Template>)
      }
      setEditing(null)
      setEditId(null)
      setUploadStatus('')
      fetchTemplates()
      onTemplatesChange()
    } catch (err) {
      alert(`保存失败: ${err instanceof Error ? err.message : '未知错误'}`)
    } finally {
      setSaving(false)
    }
  }

  // ── 启停/删除 ──────────────────────────

  const toggleActive = async (template: Template) => {
    try {
      await updateTemplate(template.id, { is_active: !template.is_active })
      fetchTemplates()
      onTemplatesChange()
    } catch (err) {
      alert(`操作失败: ${err instanceof Error ? err.message : '未知错误'}`)
    }
  }

  const handleDelete = async (template: Template) => {
    if (!confirm(`确定删除模板「${template.name}」？`)) return
    try {
      await deleteTemplate(template.id)
      fetchTemplates()
      onTemplatesChange()
    } catch (err) {
      alert(`删除失败: ${err instanceof Error ? err.message : '未知错误'}`)
    }
  }

  // ── 字段编辑 ────────────────────────────

  const updateField = (index: number, patch: Partial<TemplateField>) => {
    if (!editing) return
    const fields = [...editing.input_schema.fields]
    fields[index] = { ...fields[index], ...patch }
    setEditing({ ...editing, input_schema: { fields } })
  }

  const addField = () => {
    if (!editing) return
    setEditing({
      ...editing,
      input_schema: {
        fields: [
          ...editing.input_schema.fields,
          { name: '', label: '', type: 'text', required: false },
        ],
      },
    })
  }

  // ── 能力字段带入 ────────────────────────

  // 能力声明（FieldSpec）→ 表单字段（TemplateField）：
  // 类型对不上就找最接近的渲染形态；值形态差异在选项枚举里消化（选项值=API 原生值）
  const capabilitiesToFields = (specs: FieldSpec[]): TemplateField[] =>
    specs.map((spec) => {
      // 选项 = 下拉的选项。enum 用声明的 options；bool 没有 options 声明，
      // 但要能选，所以补 true/false（boolean 原值，不是字符串，否则下游 bool("false") 会变 true）
      const options: TemplateField['options'] | undefined = spec.options?.length
        ? [...spec.options]
        : spec.type === 'bool'
          ? [true, false]
          : undefined

      let type: TemplateField['type']
      if (options) type = 'select'                         // enum / bool：下拉，选项值即 API 原生值
      else if (spec.type === 'image') type = 'image_upload'
      else if (spec.type === 'int' || spec.type === 'float') type = 'number'
      else if (spec.name === 'prompt' || /[\r\n]/.test(spec.description || '')) type = 'textarea'
      else type = 'text'
      return {
        name: spec.name,
        label: spec.label || spec.name,
        type,
        required: !!spec.required,
        ...(options ? { options } : {}),
        ...(spec.default != null ? { default: spec.default as string | number | boolean } : {}),
        ...(spec.description ? { placeholder: spec.description } : {}),
        // 多值图片字段：一个框可多选，表单值变数组（值形态由能力声明决定，前端不猜）
        ...(spec.multiple ? { multiple: true } : {}),
      }
    })

  // 从当前选中的能力带入字段（替换现有字段，先确认防误点）
  const importCapabilityFields = () => {
    if (!editing) return
    const caps = plugins.find((p) => p.name === editing.engine)?.capabilities || {}
    const spec = caps[editing.capability]
    if (!spec) return
    if (editing.input_schema.fields.length > 0 &&
        !confirm('带入能力字段会替换当前已配置的字段，继续？')) return
    setEditing({ ...editing, input_schema: { fields: capabilitiesToFields(spec.fields) } })
  }

  const removeField = (index: number) => {
    if (!editing) return
    const fields = editing.input_schema.fields.filter((_, i) => i !== index)
    setEditing({ ...editing, input_schema: { fields } })
  }

  // 选项解析："512, 768, 1024" -> [512, 768, 1024]（纯数字转 number）
  const parseOptions = (raw: string): (string | number)[] =>
    raw
      .split(',')
      .map((s) => s.trim())
      .filter(Boolean)
      .map((s) => (/^-?\d+(\.\d+)?$/.test(s) ? Number(s) : s))

  // ── 参数映射编辑 ────────────────────────

  const updateMapping = (field: string, nodePath: string) => {
    if (!editing) return
    const mapping = { ...editing.param_mapping }
    if (nodePath.trim()) {
      mapping[field] = nodePath.trim()
    } else {
      delete mapping[field]
    }
    setEditing({ ...editing, param_mapping: mapping })
  }

  // 可注入输入按节点分组，给下拉的 <optgroup> 用 —— 同一节点的输入排在一起
  const inputGroups = useMemo(() => {
    const groups: { node_id: string; label: string; items: WorkflowInput[] }[] = []
    const byId = new Map<string, { node_id: string; label: string; items: WorkflowInput[] }>()
    for (const input of inventory?.inputs ?? []) {
      let group = byId.get(input.node_id)
      if (!group) {
        group = {
          node_id: input.node_id,
          label: `${input.node_title} · ${input.class_type}`,
          items: [],
        }
        byId.set(input.node_id, group)
        groups.push(group)
      }
      group.items.push(input)
    }
    return groups
  }, [inventory])

  const knownInputPaths = useMemo(
    () => new Set((inventory?.inputs ?? []).map((i) => i.path)),
    [inventory],
  )

  // ── 出图节点勾选 ────────────────────────

  const selectedOutputNodes = editing?.output_config.nodes ?? []

  const toggleOutputNode = (nodeId: string, on: boolean) => {
    if (!editing) return
    const next = new Set(editing.output_config.nodes ?? [])
    if (on) next.add(nodeId)
    else next.delete(nodeId)
    setEditing({ ...editing, output_config: { nodes: [...next] } })
  }

  // 打开编辑面板：顺带把这份工作流的清单拉出来，映射与勾选才有得选
  const openEditor = (form: TemplateFormData, id: string | null) => {
    setEditing(form)
    setEditId(id)
    setUploadStatus('')
    setUploadTone('accent')
    loadInventory(form.workflow_file)
    window.scrollTo({ top: 0, behavior: 'smooth' })
  }

  return (
    <div>
      <div className="rule-b flex items-center justify-between gap-md pb-sm">
        <h2 className="font-display text-section">模板管理</h2>
        <button
          onClick={() =>
            openEditor(
              {
                ...emptyForm,
                input_schema: { fields: [...emptyForm.input_schema.fields] },
                param_mapping: {},
                output_config: { nodes: [] },
              },
              null,
            )
          }
          className="btn"
        >
          新建模板
        </button>
      </div>

      {/* 编辑表单 */}
      {editing && (
        <section className="mt-lg border border-rule bg-paper-2 p-lg">
          <h3 className="font-display text-title">
            {editId ? '编辑模板' : '新建模板'}
          </h3>

          <div className="mt-md grid grid-cols-1 gap-md md:grid-cols-2">
            {/* 基本信息 */}
            <label className="block">
              <span className="field-label">模板名称 *</span>
              <input
                type="text"
                className="field"
                value={editing.name}
                onChange={(e) => setEditing({ ...editing, name: e.target.value })}
                placeholder="如：Flux2 人脸参考文生图"
              />
            </label>
            <label className="block">
              <span className="field-label">分类</span>
              <select
                className="field"
                value={editing.category}
                onChange={(e) => setEditing({ ...editing, category: e.target.value })}
              >
                <option value="image">图像</option>
                <option value="video">视频</option>
                <option value="text">文本</option>
              </select>
            </label>
            <label className="block">
              <span className="field-label">生成引擎</span>
              <select
                className="field"
                value={editing.engine}
                onChange={(e) =>
                  setEditing({ ...editing, engine: e.target.value, capability: '' })
                }
              >
                {(plugins.length > 0
                  ? plugins.map((p) => p.name)
                  : ['comfyui', 'generic_api']
                ).map((name) => (
                  <option key={name} value={name}>
                    {plugins.find((p) => p.name === name)?.label || name}（{name}）
                  </option>
                ))}
                {!plugins.some((p) => p.name === editing.engine) &&
                  !['comfyui', 'generic_api'].includes(editing.engine) && (
                    <option value={editing.engine}>{editing.engine}</option>
                  )}
              </select>
            </label>
            <label className="block">
              <span className="field-label">能力</span>
              <select
                className="field"
                value={editing.capability}
                onChange={(e) => setEditing({ ...editing, capability: e.target.value })}
              >
                <option value="">不指定（按分类）</option>
                {Object.values(
                  plugins.find((p) => p.name === editing.engine)?.capabilities || {},
                ).map((c) => (
                  <option key={c.kind} value={c.kind}>{c.label}（{c.kind}）</option>
                ))}
              </select>
            </label>
            <label className="block">
              <span className="field-label">图标</span>
              <input
                type="text"
                className="field"
                value={editing.icon}
                onChange={(e) => setEditing({ ...editing, icon: e.target.value })}
                placeholder="留空即可"
              />
            </label>
            <label className="block">
              <span className="field-label">排序</span>
              <input
                type="number"
                className="field"
                value={editing.sort_order}
                onChange={(e) => setEditing({ ...editing, sort_order: Number(e.target.value) })}
              />
            </label>
            <label className="block md:col-span-2">
              <span className="field-label">描述</span>
              <textarea
                className="field"
                rows={2}
                value={editing.description}
                onChange={(e) => setEditing({ ...editing, description: e.target.value })}
                placeholder="面向用户的场景描述"
              />
            </label>
          </div>

          {/* ComfyUI 专属：工作流上传与出图节点（插件引擎不需要） */}
          {editing.engine !== 'comfyui' && (
            <div className="rule-t mt-lg pt-lg">
              <p className="text-sm text-muted">
                该引擎不需要工作流与参数映射 —— 表单字段名就是 API 参数名，值域由能力声明的选项决定，
                调用细节写在引擎插件的 build_payload / submit 里。
              </p>
            </div>
          )}
          {editing.engine === 'comfyui' && (
          <>
          {/* 工作流上传 */}
          <div className="rule-t mt-lg pt-lg">
            <span className="field-label">ComfyUI 工作流 JSON</span>
            <div className="flex flex-wrap items-center gap-sm">
              <input
                type="file"
                accept=".json"
                className="field flex-1"
                onChange={(e) => {
                  const file = e.target.files?.[0]
                  if (file) handleWorkflowUpload(file)
                }}
              />
              {editing.workflow_file && (
                <span className="chip" data-tone="success">{editing.workflow_file}</span>
              )}
            </div>
            {uploadStatus && (
              <p role="status" className={`mt-xs text-sm ${uploadTone === 'error' ? 'text-error' : 'text-ink-2'}`}>
                {uploadStatus}
              </p>
            )}
          </div>

          {/* 出图节点勾选 */}
          <div className="rule-t mt-lg pt-lg">
            <h4 className="font-display text-title">保留哪些出图节点的结果</h4>
            <p className="mt-xs text-sm text-muted">
              一个工作流往往有多个出图节点（预览图、裁剪图…）。只勾需要的，任务就只保存它们的产出；至少勾一个才能保存。
            </p>
            {inventoryLoading ? (
              <div className="mt-sm space-y-xs" aria-hidden="true">
                <div className="skeleton h-row w-full" />
                <div className="skeleton h-row w-2/3" />
              </div>
            ) : inventoryError ? (
              <div className="mt-sm border border-error px-md py-lg">
                <p className="text-sm text-error">工作流解析失败</p>
                <p className="mt-2xs text-sm text-ink-2">{inventoryError}</p>
                {editing.workflow_file && (
                  <button onClick={() => loadInventory(editing.workflow_file)} className="btn mt-sm">
                    重新解析
                  </button>
                )}
              </div>
            ) : !inventory ? (
              <p className="mt-sm border border-dashed border-rule px-md py-lg text-center text-sm text-muted">
                先在上面上传工作流 JSON，这里会列出它的出图节点
              </p>
            ) : inventory.outputs.length === 0 ? (
              <p className="mt-sm border border-error px-md py-lg text-sm text-error">
                这个工作流里没检测到出图节点（SaveImage / PreviewImage 等）。请回 ComfyUI 确认它真的会产出结果，否则任务跑完也不会有素材。
              </p>
            ) : (
              <div className="mt-sm flex flex-wrap gap-md">
                {inventory.outputs.map((output) => (
                  <label key={output.node_id} className="check-row">
                    <input
                      type="checkbox"
                      checked={selectedOutputNodes.includes(output.node_id)}
                      onChange={(e) => toggleOutputNode(output.node_id, e.target.checked)}
                    />
                    <span className="text-ink">{output.title}</span>
                    <code className="code">{output.node_id}</code>
                    <span className="text-sm text-muted">{output.class_type}</span>
                  </label>
                ))}
              </div>
            )}
          </div>
          </>
          )}

          {/* 表单字段配置 */}
          <div className="rule-t mt-lg pt-lg">
            <div className="flex items-center justify-between gap-md">
              <h4 className="font-display text-title">输入表单字段</h4>
              <div className="flex items-center gap-sm">
                {(plugins.find((p) => p.name === editing.engine)?.capabilities || {})[
                  editing.capability
                ] && (
                  <button onClick={importCapabilityFields} className="btn-quiet">
                    从能力带入
                  </button>
                )}
                <button onClick={addField} className="btn-quiet">添加字段</button>
              </div>
            </div>
            <div className="mt-sm">
              {editing.input_schema.fields.length === 0 ? (
                <p className="border border-dashed border-rule px-md py-lg text-center text-sm text-muted">
                  还没有字段，点「添加字段」开始定义表单
                </p>
              ) : (
                editing.input_schema.fields.map((field, i) => (
                  <div key={i} className="rule-b py-sm">
                    <div className="flex flex-wrap items-end gap-sm">
                      <label className="block w-28">
                        <span className="field-label">字段名</span>
                        <input
                          className="field"
                          placeholder="prompt"
                          value={field.name}
                          onChange={(e) => updateField(i, { name: e.target.value })}
                        />
                      </label>
                      <label className="block w-28">
                        <span className="field-label">显示名</span>
                        <input
                          className="field"
                          placeholder="提示词"
                          value={field.label}
                          onChange={(e) => updateField(i, { label: e.target.value })}
                        />
                      </label>
                      <label className="block">
                        <span className="field-label">类型</span>
                        <select
                          className="field"
                          value={field.type}
                          onChange={(e) => updateField(i, { type: e.target.value as TemplateField['type'] })}
                        >
                          <option value="text">文本</option>
                          <option value="textarea">多行文本</option>
                          <option value="number">数字</option>
                          <option value="select">下拉</option>
                          <option value="image_upload">图片上传</option>
                        </select>
                      </label>
                      <label className="check-row">
                        <input
                          type="checkbox"
                          checked={field.required}
                          onChange={(e) => updateField(i, { required: e.target.checked })}
                        />
                        必填
                      </label>
                      <label className="check-row">
                        <input
                          type="checkbox"
                          checked={!!field.advanced}
                          onChange={(e) => updateField(i, { advanced: e.target.checked })}
                        />
                        高级
                      </label>
                      <button
                        onClick={() => removeField(i)}
                        className="x-btn ml-auto"
                        aria-label={`删除字段 ${field.name || i + 1}`}
                      >
                        ✕
                      </button>
                    </div>

                    {/* 字段附加配置：默认值 / 占位提示 / 下拉选项 */}
                    <div className="mt-xs flex flex-wrap items-end gap-sm">
                      <label className="block w-28">
                        <span className="field-label">默认值</span>
                        <input
                          className="field"
                          value={String(field.default ?? '')}
                          onChange={(e) => updateField(i, { default: e.target.value || undefined })}
                        />
                      </label>
                      <label className="block w-36">
                        <span className="field-label">占位提示</span>
                        <input
                          className="field"
                          value={field.placeholder ?? ''}
                          onChange={(e) => updateField(i, { placeholder: e.target.value || undefined })}
                        />
                      </label>
                      {field.type === 'select' && (
                        <label className="block min-w-40 flex-1">
                          <span className="field-label">选项（逗号分隔，失焦生效）</span>
                          <input
                            key={`options-${i}-${field.name}`}
                            className="field"
                            placeholder="如：512, 768, 1024"
                            defaultValue={(field.options ?? []).join(', ')}
                            onBlur={(e) => updateField(i, { options: parseOptions(e.target.value) })}
                          />
                        </label>
                      )}
                    </div>
                  </div>
                ))
              )}
            </div>
          </div>

          {/* 参数映射（ComfyUI 专属：把表单字段注入工作流节点） */}
          {editing.engine === 'comfyui' && (
          <div className="rule-t mt-lg pt-lg">
            <h4 className="font-display text-title">参数映射（表单字段 → 工作流输入）</h4>
            <div className="mt-sm">
              {editing.input_schema.fields.filter((f) => f.name).length === 0 ? (
                <p className="border border-dashed border-rule px-md py-lg text-center text-sm text-muted">
                  先在上面给字段起名，再在这里映射到工作流节点
                </p>
              ) : !inventory ? (
                <p className="border border-dashed border-rule px-md py-lg text-center text-sm text-muted">
                  先在上面上传工作流 JSON，这里会列出每个节点可注入的输入
                </p>
              ) : (
                editing.input_schema.fields
                  .filter((f) => f.name)
                  .map((field) => {
                    const current = editing.param_mapping[field.name] || ''
                    // 老映射可能指向已经不存在的输入 —— 保留这个选项让用户看见，
                    // 而不是让下拉默默跳回「不映射」
                    const orphan = current && !knownInputPaths.has(current)
                    return (
                      <div key={field.name} className="rule-b flex flex-wrap items-end gap-sm py-xs">
                        <span className="chip w-32 truncate">{field.label || field.name}</span>
                        <span aria-hidden="true" className="text-sm text-muted">→</span>
                        <label className="block min-w-40 flex-1">
                          <span className="visually-hidden">{field.label || field.name} 映射到的工作流输入</span>
                          <select
                            className="field"
                            value={current}
                            disabled={inventoryLoading || inventory.inputs.length === 0}
                            onChange={(e) => updateMapping(field.name, e.target.value)}
                          >
                            <option value="">不映射</option>
                            {orphan && (
                              <option value={current}>{current}（工作流里已找不到，建议重选）</option>
                            )}
                            {inputGroups.map((group) => (
                              <optgroup key={group.node_id} label={group.label}>
                                {group.items.map((item) => (
                                  <option key={item.path} value={item.path}>
                                    {item.current_value
                                      ? `${item.input_name}（${item.current_value}）`
                                      : item.input_name}
                                  </option>
                                ))}
                              </optgroup>
                            ))}
                          </select>
                        </label>
                      </div>
                    )
                  })
              )}
            </div>
            <p className="mt-sm text-sm text-muted">
              下拉里按节点分组，只列能注入的输入（文本 / 数字这类字面量）；节点之间的连线属于图结构，改不了也不该改。
            </p>
          </div>
          )}

          {/* 操作 */}
          <div className="rule-t mt-lg flex gap-sm pt-lg">
            <button
              onClick={handleSave}
              disabled={saving}
              className="btn btn-primary"
            >
              {saving ? '保存中…' : editId ? '保存修改' : '创建模板'}
            </button>
            <button
              onClick={() => { setEditing(null); setEditId(null) }}
              className="btn"
            >
              取消
            </button>
          </div>
        </section>
      )}

      {/* 模板列表 */}
      <section className="mt-xl">
        <h3 className="font-display text-section">已配置的模板</h3>

        {loading ? (
          <div className="mt-md space-y-xs" aria-hidden="true">
            {Array.from({ length: 3 }).map((_, i) => (
              <div key={i} className="flex items-center gap-md">
                <div className="skeleton h-row flex-1" />
                <div className="skeleton h-row w-24" />
                <div className="skeleton h-row w-32" />
              </div>
            ))}
          </div>
        ) : loadError ? (
          <div className="mt-md border border-error px-md py-lg">
            <p className="text-base text-error">模板列表加载失败</p>
            <p className="mt-2xs text-sm text-ink-2">{loadError}</p>
            <button onClick={fetchTemplates} className="btn mt-sm">重新加载</button>
          </div>
        ) : templates.length === 0 ? (
          <p className="mt-md border border-dashed border-rule px-md py-xl text-center text-base text-muted">
            还没有模板，点右上角「新建模板」创建第一个
          </p>
        ) : (
          <table className="table mt-md">
            <caption className="visually-hidden">模板列表</caption>
            <thead>
              <tr>
                <th scope="col">模板</th>
                <th scope="col">工作流</th>
                <th scope="col" className="num">参数映射</th>
                <th scope="col">操作</th>
              </tr>
            </thead>
            <tbody>
              {templates.map((template) => (
                <tr key={template.id}>
                  <td>
                    <div className="flex flex-wrap items-center gap-xs">
                      <span className="font-display text-title text-ink">{template.name}</span>
                      <span className="chip" data-tone={template.is_active ? 'success' : undefined}>
                        {template.is_active ? '已启用' : '已停用'}
                      </span>
                      <span className="chip">{categoryLabel[template.category] ?? template.category}</span>
                      <span className="chip">
                        {plugins.find((p) => p.name === template.engine)?.label || template.engine}
                      </span>
                      {template.capability && <span className="chip">{template.capability}</span>}
                    </div>
                    {template.description && (
                      <p className="mt-2xs text-sm text-muted">{template.description}</p>
                    )}
                  </td>
                  <td className="text-sm">
                    {template.workflow_file ? (
                      <code className="code">{template.workflow_file}</code>
                    ) : (
                      <span className="text-muted">未配置工作流</span>
                    )}
                  </td>
                  <td className="num">{Object.keys(template.param_mapping || {}).length}</td>
                  <td>
                    <div className="flex items-center gap-2xs">
                      <button
                        onClick={() =>
                          openEditor(
                            {
                              name: template.name,
                              description: template.description,
                              category: template.category,
                              icon: template.icon,
                              engine: template.engine,
                              capability: template.capability,
                              workflow_file: template.workflow_file,
                              input_schema: {
                                fields: JSON.parse(
                                  JSON.stringify(template.input_schema?.fields ?? []),
                                ),
                              },
                              param_mapping: { ...template.param_mapping },
                              output_config: { nodes: [...(template.output_config?.nodes ?? [])] },
                              is_active: template.is_active,
                              sort_order: template.sort_order,
                            },
                            template.id,
                          )
                        }
                        className="btn-quiet"
                      >
                        编辑
                      </button>
                      <button
                        onClick={() => toggleActive(template)}
                        className="btn-quiet"
                      >
                        {template.is_active ? '停用' : '启用'}
                      </button>
                      <button
                        onClick={() => handleDelete(template)}
                        className="btn-danger-quiet"
                      >
                        删除
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </div>
  )
}
