/** 动态表单 — 根据模板 input_schema 自动渲染 */
import { useState, useEffect, useRef } from 'react'
import type { Asset, Template, TemplateField } from '../types'
import { fetchAssetBlob, getAssetUrl, uploadFromAsset, uploadImage } from '../api/client'
import AssetPickerModal from './AssetPickerModal'

interface Props {
  template: Template
  onSubmit: (params: Record<string, unknown>, batchCount: number) => void
  submitting: boolean
  /** 预填值（「用这些参数再生成」草稿）。只在挂载时生效 ——
   *  调用方换 initialValues 时要换 key 强制重挂 */
  initialValues?: Record<string, unknown>
}

export default function DynamicForm({ template, onSubmit, submitting, initialValues }: Props) {
  const fields = template.input_schema?.fields ?? []

  // 初始化表单值：草稿预填 > 模板默认值 > 空
  const [values, setValues] = useState<Record<string, unknown>>(() => {
    const init: Record<string, unknown> = {}
    for (const f of fields) {
      const prefill = initialValues?.[f.name]
      init[f.name] = prefill !== undefined && prefill !== null ? prefill : (f.default ?? '')
    }
    return init
  })
  const [batchCount, setBatchCount] = useState(1)
  const [showAdvanced, setShowAdvanced] = useState(false)
  const [uploading, setUploading] = useState<Record<string, boolean>>({})
  // 本地预览：选中即出图（object URL），不用等上传返回
  const [previews, setPreviews] = useState<Record<string, string>>({})
  const [previewNames, setPreviewNames] = useState<Record<string, string>>({})
  // object URL 必须手动回收，换图/移除/卸载漏一个就漏一块内存
  const previewUrls = useRef<Record<string, string>>({})
  // 「从素材库选择」弹窗当前服务于哪个字段（null = 未打开）
  const [pickerFor, setPickerFor] = useState<TemplateField | null>(null)

  const setValue = (name: string, val: unknown) =>
    setValues((prev) => ({ ...prev, [name]: val }))

  const setPreview = (name: string, blob: Blob, fileName: string) => {
    const url = URL.createObjectURL(blob)
    const old = previewUrls.current[name]
    if (old) URL.revokeObjectURL(old)
    previewUrls.current[name] = url
    setPreviews((prev) => ({ ...prev, [name]: url }))
    setPreviewNames((prev) => ({ ...prev, [name]: fileName }))
  }

  const clearPreview = (name: string) => {
    const old = previewUrls.current[name]
    if (old) {
      URL.revokeObjectURL(old)
      delete previewUrls.current[name]
    }
    setPreviews((prev) => {
      const next = { ...prev }
      delete next[name]
      return next
    })
    setPreviewNames((prev) => {
      const next = { ...prev }
      delete next[name]
      return next
    })
  }

  // 卸载时把还挂着的 object URL 全部回收
  useEffect(
    () => () => {
      for (const url of Object.values(previewUrls.current)) URL.revokeObjectURL(url)
      previewUrls.current = {}
    },
    [],
  )

  // 上传参考图片到生成引擎，入参使用引擎侧文件名
  const handleUpload = async (field: TemplateField, file: File) => {
    setPreview(field.name, file, file.name) // 先出预览，不等网络
    setUploading((prev) => ({ ...prev, [field.name]: true }))
    try {
      const serverName = await uploadImage(file, template.engine)
      setValue(field.name, serverName)
    } catch (err) {
      // 上传失败就把预览撤掉，免得看着像已经传上去了
      clearPreview(field.name)
      alert(`上传失败: ${err instanceof Error ? err.message : err}`)
    } finally {
      setUploading((prev) => ({ ...prev, [field.name]: false }))
    }
  }

  // 从素材库选图：素材文件在本服务磁盘上，ComfyUI 是另一台机器看不见，
  // 选完仍要把字节推进引擎输入目录。产出的引擎侧文件名与本地上传一致，
  // 参数注入链路无需区分来源。
  const handlePickAsset = async (field: TemplateField, asset: Asset) => {
    setPickerFor(null)
    setUploading((prev) => ({ ...prev, [field.name]: true }))
    try {
      // 预览取不到不致命（退回只有文件名的值态），先出图再送引擎；
      // 顺序执行保证失败清预览时，不会有慢半拍的预览又冒出来
      try {
        const blob = await fetchAssetBlob(getAssetUrl(asset.task_id, asset.file_name))
        setPreview(field.name, blob, asset.file_name)
      } catch {
        // 预览失败不挡选用
      }
      const serverName = await uploadFromAsset(asset.id, template.engine)
      setValue(field.name, serverName)
    } catch (err) {
      // 推送失败就把预览撤掉，免得看着像已经选上了
      clearPreview(field.name)
      alert(`选用素材失败: ${err instanceof Error ? err.message : err}`)
    } finally {
      setUploading((prev) => ({ ...prev, [field.name]: false }))
    }
  }

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    // 过滤掉空值
    const params: Record<string, unknown> = {}
    for (const [k, v] of Object.entries(values)) {
      if (v !== '' && v !== null && v !== undefined) params[k] = v
    }
    onSubmit(params, batchCount)
  }

  const renderField = (field: TemplateField) => {
    const value = values[field.name]
    const id = `field-${field.name}`

    switch (field.type) {
      case 'textarea':
        return (
          <textarea
            id={id}
            className="field"
            rows={3}
            placeholder={field.placeholder || `请输入${field.label}`}
            value={String(value ?? '')}
            onChange={(e) => setValue(field.name, e.target.value)}
            required={field.required}
          />
        )
      case 'number':
        return (
          <input
            id={id}
            type="number"
            className="field"
            placeholder={field.placeholder || `请输入${field.label}`}
            value={String(value ?? '')}
            onChange={(e) => setValue(field.name, e.target.value ? Number(e.target.value) : '')}
            required={field.required}
          />
        )
      case 'select':
        return (
          <select
            id={id}
            className="field"
            value={String(value ?? '')}
            onChange={(e) => {
              const raw = e.target.value
              const opt = field.options?.find((o) => String(o) === raw)
              setValue(field.name, opt ?? raw)
            }}
            required={field.required}
          >
            <option value="">请选择</option>
            {field.options?.map((opt) => (
              <option key={String(opt)} value={String(opt)}>
                {opt}
              </option>
            ))}
          </select>
        )
      case 'image_upload': {
        const preview = previews[field.name]
        const previewName = previewNames[field.name]
        return (
          <div className="border border-rule bg-paper">
            <input
              type="file"
              accept="image/*"
              className="visually-hidden"
              id={`upload-${field.name}`}
              onChange={(e) => {
                const file = e.target.files?.[0]
                if (file) handleUpload(field, file)
                e.target.value = '' // 允许重复选择同一文件
              }}
            />
            {preview ? (
              <div className="flex items-start gap-sm p-sm">
                <div className="h-24 w-24 shrink-0 overflow-hidden border border-rule bg-paper-2">
                  <img
                    src={preview}
                    alt={previewName || field.label}
                    className="h-full w-full object-cover"
                  />
                </div>
                <div className="min-w-0 flex-1">
                  <p className="truncate text-sm text-ink">{previewName}</p>
                  <p className="mt-2xs text-xs text-muted">
                    {uploading[field.name]
                      ? '上传中…'
                      : value
                        ? `已上传到 ${template.engine}`
                        : '待上传'}
                  </p>
                  <div className="mt-xs flex gap-xs">
                    <label htmlFor={`upload-${field.name}`} className="btn-quiet cursor-pointer">
                      更换
                    </label>
                    <button type="button" className="btn-quiet" onClick={() => setPickerFor(field)}>
                      从素材库
                    </button>
                    <button
                      type="button"
                      className="btn-danger-quiet"
                      onClick={() => {
                        clearPreview(field.name)
                        setValue(field.name, '')
                      }}
                    >
                      移除
                    </button>
                  </div>
                </div>
              </div>
            ) : value ? (
              <div className="flex items-center gap-sm p-sm">
                <div className="min-w-0 flex-1">
                  <p className="truncate text-sm text-ink">{String(value)}</p>
                  <p className="mt-2xs text-xs text-muted">已上传到 {template.engine}</p>
                </div>
                <label htmlFor={`upload-${field.name}`} className="btn-quiet cursor-pointer">
                  更换
                </label>
                <button type="button" className="btn-quiet" onClick={() => setPickerFor(field)}>
                  从素材库
                </button>
                <button
                  type="button"
                  className="btn-danger-quiet"
                  onClick={() => setValue(field.name, '')}
                >
                  移除
                </button>
              </div>
            ) : (
              <div className="border border-dashed border-muted px-md py-lg text-center text-sm text-ink-2">
                {uploading[field.name] ? (
                  <span className="text-accent">上传中…</span>
                ) : (
                  <div className="flex flex-wrap items-center justify-center gap-sm">
                    <label htmlFor={`upload-${field.name}`} className="btn-quiet cursor-pointer">
                      点击上传{field.label}
                    </label>
                    <button type="button" className="btn-quiet" onClick={() => setPickerFor(field)}>
                      从素材库选择
                    </button>
                  </div>
                )}
              </div>
            )}
          </div>
        )
      }
      default:
        return (
          <input
            id={id}
            type="text"
            className="field"
            placeholder={field.placeholder || `请输入${field.label}`}
            value={String(value ?? '')}
            onChange={(e) => setValue(field.name, e.target.value)}
            required={field.required}
          />
        )
    }
  }

  const basicFields = fields.filter((f) => !f.advanced)
  const advancedFields = fields.filter((f) => f.advanced)

  return (
    <form onSubmit={handleSubmit}>
      {basicFields.map((field) => (
        <div key={field.name} className="mt-md">
          <label className="field-label" htmlFor={`field-${field.name}`}>
            {field.label}
            {field.required && (
              <span className="ml-2xs text-error" aria-hidden="true">
                *
              </span>
            )}
          </label>
          {renderField(field)}
        </div>
      ))}

      {advancedFields.length > 0 && (
        <div className="mt-md">
          <button
            type="button"
            className="btn-quiet"
            aria-expanded={showAdvanced}
            onClick={() => setShowAdvanced(!showAdvanced)}
          >
            {showAdvanced ? '▾ 收起高级选项' : '▸ 展开高级选项'}
          </button>
          {showAdvanced && (
            <div className="mt-sm border border-rule bg-paper p-md">
              {advancedFields.map((field) => (
                <div key={field.name} className="mt-md first:mt-0">
                  <label className="field-label" htmlFor={`field-${field.name}`}>
                    {field.label}
                  </label>
                  {renderField(field)}
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      <div className="mt-lg flex flex-wrap items-end gap-sm border-t border-rule pt-md">
        <div>
          <label className="field-label" htmlFor="batchCount">
            生成数量
          </label>
          <input
            id="batchCount"
            type="number"
            min={1}
            max={32}
            className="field w-20"
            value={batchCount}
            onChange={(e) => setBatchCount(Math.max(1, Math.min(32, Number(e.target.value))))}
          />
        </div>
        <button
          type="submit"
          disabled={submitting}
          className="btn btn-primary flex-1 justify-center"
        >
          {submitting ? '提交中…' : '开始生成'}
        </button>
      </div>

      {/* 素材选择弹窗（fixed 遮罩，不影响表单布局） */}
      {pickerFor && (
        <AssetPickerModal
          kind="image"
          onSelect={(asset) => handlePickAsset(pickerFor, asset)}
          onClose={() => setPickerFor(null)}
        />
      )}
    </form>
  )
}
