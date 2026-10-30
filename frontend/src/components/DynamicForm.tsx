/** 动态表单 — 根据模板 input_schema 自动渲染 */
import { useState, useEffect, useRef } from 'react'
import type { Asset, Template, TemplateField } from '../types'
import { fetchAssetBlob, getAssetUrl, uploadFromAsset, uploadInput } from '../api/client'
import AssetPickerModal from './AssetPickerModal'

interface Props {
  template: Template
  onSubmit: (params: Record<string, unknown>, batchCount: number) => void
  submitting: boolean
  /** 预填值（「用这些参数再生成」草稿）。只在挂载时生效 ——
   *  调用方换 initialValues 时要换 key 强制重挂 */
  initialValues?: Record<string, unknown>
}

/** 文件种类 → 扩展名兜底表。
 *  有些系统/浏览器给的 file.type 是空的（比如某些 mp3），只靠 MIME 会误判 */
const KIND_EXT: Record<string, string[]> = {
  image: ['png', 'jpg', 'jpeg', 'jfif', 'webp', 'gif', 'bmp'],
  audio: ['mp3', 'wav', 'm4a', 'aac', 'flac', 'ogg', 'opus'],
  video: ['mp4', 'webm', 'mov', 'mkv'],
}

/** 选中的文件是不是这个字段声明的种类。
 *  `<input accept>` 只是文件对话框的提示，用户可以切到「所有文件」绕过，
 *  所以落盘前必须自己再拦一次 —— 否则音频字段能塞进图片，到插件才炸。 */
const matchesKind = (file: File, kind: string): boolean => {
  const mimeKind = (file.type || '').split('/')[0]
  if (mimeKind) return mimeKind === kind
  const ext = (file.name.split('.').pop() || '').toLowerCase()
  return (KIND_EXT[kind] ?? []).includes(ext)
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
  // 本地预览：选中即出图（object URL），不用等上传返回。
  // 统一用数组存 —— 单图字段长度恒为 1，多图字段（field.multiple）逐张累加
  const [previews, setPreviews] = useState<Record<string, string[]>>({})
  const [previewNames, setPreviewNames] = useState<Record<string, string[]>>({})
  // object URL 必须手动回收，换图/移除/卸载漏一个就漏一块内存
  const previewUrls = useRef<Record<string, string[]>>({})
  // 「从素材库选择」弹窗当前服务于哪个字段（null = 未打开）
  const [pickerFor, setPickerFor] = useState<TemplateField | null>(null)

  const setValue = (name: string, val: unknown) =>
    setValues((prev) => ({ ...prev, [name]: val }))

  /** 单图字段：替换预览（旧的 object URL 立即回收） */
  const setSinglePreview = (name: string, blob: Blob, fileName: string) => {
    for (const url of previewUrls.current[name] ?? []) URL.revokeObjectURL(url)
    const url = URL.createObjectURL(blob)
    previewUrls.current[name] = [url]
    setPreviews((prev) => ({ ...prev, [name]: [url] }))
    setPreviewNames((prev) => ({ ...prev, [name]: [fileName] }))
  }

  /** 多图字段：追加一张预览（与 values 里的顺序对齐） */
  const appendPreview = (name: string, blob: Blob, fileName: string) => {
    const url = URL.createObjectURL(blob)
    previewUrls.current[name] = [...(previewUrls.current[name] ?? []), url]
    setPreviews((prev) => ({ ...prev, [name]: [...(prev[name] ?? []), url] }))
    setPreviewNames((prev) => ({ ...prev, [name]: [...(prev[name] ?? []), fileName] }))
  }

  /** 多图字段：按位置移除一张预览 */
  const removePreviewAt = (name: string, index: number) => {
    const urls = previewUrls.current[name] ?? []
    if (urls[index]) URL.revokeObjectURL(urls[index])
    previewUrls.current[name] = urls.filter((_, i) => i !== index)
    setPreviews((prev) => ({ ...prev, [name]: (prev[name] ?? []).filter((_, i) => i !== index) }))
    setPreviewNames((prev) => ({ ...prev, [name]: (prev[name] ?? []).filter((_, i) => i !== index) }))
  }

  /** 多图字段：删掉第 index 张 —— 预览与表单值同步移除（上传未完成的图值不存在，只删预览） */
  const removeImageAt = (field: TemplateField, index: number) => {
    removePreviewAt(field.name, index)
    setValues((prev) => {
      const cur = Array.isArray(prev[field.name]) ? (prev[field.name] as string[]) : []
      if (index >= cur.length) return prev
      return { ...prev, [field.name]: cur.filter((_, i) => i !== index) }
    })
  }

  const clearPreviews = (name: string) => {
    for (const url of previewUrls.current[name] ?? []) URL.revokeObjectURL(url)
    delete previewUrls.current[name]
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
      for (const urls of Object.values(previewUrls.current)) {
        for (const url of urls) URL.revokeObjectURL(url)
      }
      previewUrls.current = {}
    },
    [],
  )

  // 上传参考文件到生成引擎，入参使用引擎侧文件名。
  // 多文件字段（field.multiple）逐个串行上传后追加进数组，失败只回滚那一个。
  const handleUpload = async (field: TemplateField, incoming: File[]) => {
    // 上传种类由字段声明决定（image/audio/video），前端不再假设「上传 = 图片」
    const kind = field.accept ?? 'image'
    // accept 属性拦不住「所有文件」，这里按声明的种类再筛一遍
    const files = incoming.filter((f) => matchesKind(f, kind))
    if (files.length < incoming.length) {
      const label = kind === 'audio' ? '音频' : kind === 'video' ? '视频' : '图片'
      alert(
        `已忽略 ${incoming.length - files.length} 个非${label}文件：\n` +
          incoming
            .filter((f) => !matchesKind(f, kind))
            .map((f) => f.name)
            .join('\n'),
      )
    }
    if (files.length === 0) return

    if (!field.multiple) {
      const file = files[0]
      setSinglePreview(field.name, file, file.name) // 先出预览，不等网络
      setUploading((prev) => ({ ...prev, [field.name]: true }))
      try {
        const serverName = await uploadInput(file, template.engine, kind)
        setValue(field.name, serverName)
      } catch (err) {
        // 上传失败就把预览撤掉，免得看着像已经传上去了
        clearPreviews(field.name)
        alert(`上传失败: ${err instanceof Error ? err.message : err}`)
      } finally {
        setUploading((prev) => ({ ...prev, [field.name]: false }))
      }
      return
    }

    setUploading((prev) => ({ ...prev, [field.name]: true }))
    // 串行上传：顺序即用户选择顺序。nextIndex 跟着数组实际长度走，
    // 某张失败被移除后，后面的图仍然落在正确的预览位置上
    let nextIndex = previews[field.name]?.length ?? 0
    for (const file of files) {
      appendPreview(field.name, file, file.name)
      const myIndex = nextIndex
      try {
        const serverName = await uploadInput(file, template.engine, kind)
        setValues((prev) => {
          const cur = Array.isArray(prev[field.name]) ? (prev[field.name] as string[]) : []
          return { ...prev, [field.name]: [...cur, serverName] }
        })
        nextIndex = myIndex + 1
      } catch (err) {
        removePreviewAt(field.name, myIndex)
        alert(`上传失败: ${err instanceof Error ? err.message : err}`)
      }
    }
    setUploading((prev) => ({ ...prev, [field.name]: false }))
  }

  // 从素材库选素材：素材文件在本服务磁盘上，ComfyUI 是另一台机器看不见，
  // 选完仍要把字节推进引擎输入目录。产出的引擎侧文件名与本地上传一致，
  // 参数注入链路无需区分来源。
  // 列表字段可一次选多个 —— 逐个串行推送，顺序即勾选顺序。
  const handlePickAssets = async (field: TemplateField, assets: Asset[]) => {
    setPickerFor(null)
    if (assets.length === 0) return
    const multi = !!field.multiple
    const picked = multi ? assets : assets.slice(0, 1)
    setUploading((prev) => ({ ...prev, [field.name]: true }))
    // 与 handleUpload 同理：nextIndex 跟着数组实际长度走，
    // 某个失败被移除后，后面的素材仍落在正确的预览位置上
    let nextIndex = previews[field.name]?.length ?? 0
    let failed = 0
    try {
      for (const asset of picked) {
        const myIndex = multi ? nextIndex : 0
        let previewShown = false
        try {
          // 预览取不到不致命（退回只有文件名的值态），先出图再送引擎；
          // 顺序执行保证失败清预览时，不会有慢半拍的预览又冒出来
          try {
            const blob = await fetchAssetBlob(getAssetUrl(asset.task_id, asset.file_name))
            if (multi) appendPreview(field.name, blob, asset.file_name)
            else setSinglePreview(field.name, blob, asset.file_name)
            previewShown = true
          } catch {
            // 预览失败不挡选用
          }
          const serverName = await uploadFromAsset(asset.id, template.engine)
          if (multi) {
            setValues((prev) => {
              const cur = Array.isArray(prev[field.name]) ? (prev[field.name] as string[]) : []
              return { ...prev, [field.name]: [...cur, serverName] }
            })
            nextIndex = myIndex + 1
          } else {
            setValue(field.name, serverName)
          }
        } catch {
          // 推送失败就把这次加的预览撤掉，免得看着像已经选上了
          if (multi) {
            if (previewShown) removePreviewAt(field.name, myIndex)
          } else {
            clearPreviews(field.name)
          }
          failed++
        }
      }
    } finally {
      setUploading((prev) => ({ ...prev, [field.name]: false }))
    }
    // 批量时只在最后报一次总数，别一个个弹
    if (failed) {
      alert(
        `${failed} 个素材选用失败（其余 ${picked.length - failed} 个已加入）\n` +
          '常见原因：素材文件已从磁盘删除，或引擎不可达。',
      )
    }
  }

  // ── 列表字段（multiple）：值与元素类型无关，永远是数组 ──────────
  const listValue = (name: string): unknown[] => {
    const v = values[name]
    return Array.isArray(v) ? v : []
  }

  const setListItem = (name: string, index: number, val: unknown) =>
    setValues((prev) => {
      const cur = Array.isArray(prev[name]) ? [...(prev[name] as unknown[])] : []
      cur[index] = val
      return { ...prev, [name]: cur }
    })

  const addListItem = (name: string) =>
    setValues((prev) => {
      const cur = Array.isArray(prev[name]) ? [...(prev[name] as unknown[])] : []
      return { ...prev, [name]: [...cur, ''] }
    })

  const removeListItem = (name: string, index: number) =>
    setValues((prev) => {
      const cur = Array.isArray(prev[name]) ? [...(prev[name] as unknown[])] : []
      return { ...prev, [name]: cur.filter((_, i) => i !== index) }
    })

  const toggleListItem = (name: string, option: unknown) =>
    setValues((prev) => {
      const cur = Array.isArray(prev[name]) ? [...(prev[name] as unknown[])] : []
      const exists = cur.some((x) => String(x) === String(option))
      return {
        ...prev,
        [name]: exists ? cur.filter((x) => String(x) !== String(option)) : [...cur, option],
      }
    })

  /** 标量列表：可增删的多行输入；枚举列表：多选勾选组 */
  const renderListField = (field: TemplateField) => {
    const items = listValue(field.name)

    if (field.type === 'select') {
      return (
        <div className="flex flex-wrap gap-sm border border-rule bg-paper p-sm">
          {field.options?.map((opt) => (
            <label key={String(opt)} className="check-row">
              <input
                type="checkbox"
                checked={items.some((x) => String(x) === String(opt))}
                onChange={() => toggleListItem(field.name, opt)}
              />
              {typeof opt === 'boolean' ? (opt ? '是' : '否') : opt}
            </label>
          ))}
        </div>
      )
    }

    return (
      <div className="flex flex-col gap-2xs">
        {items.length === 0 && (
          <p className="text-xs text-muted">还没有添加{field.label}（可添加多个，按顺序提交）</p>
        )}
        {items.map((item, i) => (
          <div key={i} className="flex items-center gap-xs">
            <input
              className="field flex-1"
              type={field.type === 'number' ? 'number' : 'text'}
              placeholder={field.placeholder || `第 ${i + 1} 项`}
              value={String(item ?? '')}
              onChange={(e) =>
                setListItem(
                  field.name,
                  i,
                  field.type === 'number' && e.target.value !== ''
                    ? Number(e.target.value)
                    : e.target.value,
                )
              }
            />
            <button
              type="button"
              className="x-btn"
              aria-label={`删除第 ${i + 1} 项`}
              onClick={() => removeListItem(field.name, i)}
            >
              ✕
            </button>
          </div>
        ))}
        <button type="button" className="btn-quiet self-start" onClick={() => addListItem(field.name)}>
          添加一项
        </button>
      </div>
    )
  }

  /** 音频 / 视频上传：不做图片网格，按条目显示文件名（音频带播放器便于确认选对了） */
  const renderAvField = (field: TemplateField, kind: 'audio' | 'video') => {
    const isMulti = !!field.multiple
    const urls = previews[field.name] ?? []
    const names = previewNames[field.name] ?? []
    const busy = !!uploading[field.name]
    const unit = kind === 'audio' ? '音频' : '视频'
    const uploadId = `upload-${field.name}`
    const rows = (isMulti ? names : names.slice(0, 1)).map((n, i) => ({ n, i }))

    return (
      <div className="border border-rule bg-paper">
        <input
          type="file"
          accept={`${kind}/*`}
          multiple={isMulti}
          className="visually-hidden"
          id={uploadId}
          onChange={(e) => {
            const files = Array.from(e.target.files ?? [])
            if (files.length) handleUpload(field, files)
            e.target.value = '' // 允许重复选择同一文件
          }}
        />
        <div className="p-sm">
          {rows.length === 0 ? (
            <div className="border border-dashed border-muted px-md py-lg text-center text-sm text-ink-2">
              {busy ? '上传中…' : `还没有选择${unit}`}
            </div>
          ) : (
            <ul className="flex flex-col gap-2xs">
              {rows.map(({ n, i }) => (
                <li
                  key={`${n}-${i}`}
                  className="flex items-center gap-sm border border-rule bg-paper-2 px-sm py-2xs"
                >
                  {kind === 'audio' && urls[i] ? (
                    <audio src={urls[i]} controls className="h-8 max-w-64" aria-label={n} />
                  ) : (
                    <span className="min-w-0 flex-1 truncate text-sm text-ink">
                      {n || `${unit} ${i + 1}`}
                    </span>
                  )}
                  <button
                    type="button"
                    className="btn-danger-quiet"
                    disabled={busy}
                    onClick={() => {
                      if (isMulti) {
                        removeImageAt(field, i)
                      } else {
                        clearPreviews(field.name)
                        setValue(field.name, '')
                      }
                    }}
                  >
                    移除
                  </button>
                </li>
              ))}
            </ul>
          )}
          <div className="mt-xs flex flex-wrap items-center gap-sm">
            <label
              htmlFor={uploadId}
              className={`btn-quiet cursor-pointer ${busy ? 'pointer-events-none opacity-50' : ''}`}
            >
              {rows.length ? `继续添加${unit}` : `上传${unit}`}
            </label>
            <button
              type="button"
              className="btn-quiet"
              disabled={busy}
              onClick={() => setPickerFor(field)}
            >
              从素材库
            </button>
            <span className="text-xs text-muted">
              {busy ? '上传中…' : rows.length ? `已选 ${rows.length} 个` : '未选择'}
            </span>
          </div>
        </div>
      </div>
    )
  }

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    // 过滤掉空值（多图字段的 [] 也算空）
    const params: Record<string, unknown> = {}
    for (const [k, v] of Object.entries(values)) {
      if (v === '' || v === null || v === undefined) continue
      if (Array.isArray(v)) {
        // 列表字段：去掉空行，空列表整体不发（让插件的必填校验报出人话）
        const items = v.filter((x) => x !== '' && x !== null && x !== undefined)
        if (items.length === 0) continue
        params[k] = items
        continue
      }
      params[k] = v
    }
    onSubmit(params, batchCount)
  }

  const renderField = (field: TemplateField) => {
    const value = values[field.name]
    const id = `field-${field.name}`

    // 上传控件：kind 由字段声明决定（image / audio / video），image_upload 是历史别名
    const fileKind =
      field.type === 'file_upload' || field.type === 'image_upload'
        ? (field.accept ?? 'image')
        : null

    // 音频 / 视频：不做图片网格，走条目式渲染（图片仍走下面的缩略图网格）
    if (fileKind && fileKind !== 'image') return renderAvField(field, fileKind)

    // 列表字段（非文件）：值永远是数组，控件按元素类型渲染
    if (field.multiple && !fileKind) return renderListField(field)

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
                {/* boolean 选项直接当 children 会被 React 渲染成空，必须显式转文本 */}
                {typeof opt === 'boolean' ? (opt ? '是' : '否') : opt}
              </option>
            ))}
          </select>
        )
      case 'image_upload':
      case 'file_upload': {
        const isMulti = !!field.multiple
        const urls = previews[field.name] ?? []
        const names = previewNames[field.name] ?? []
        const preview = urls[0]
        const previewName = names[0]
        return (
          <div className="border border-rule bg-paper">
            <input
              type="file"
              accept="image/*"
              multiple={isMulti}
              className="visually-hidden"
              id={`upload-${field.name}`}
              onChange={(e) => {
                const files = Array.from(e.target.files ?? [])
                if (files.length) handleUpload(field, files)
                e.target.value = '' // 允许重复选择同一文件
              }}
            />
            {isMulti ? (
              <div className="p-sm">
                {urls.length > 0 ? (
                  <div className="flex flex-wrap gap-sm">
                    {urls.map((url, i) => (
                      <div
                        key={`${url}-${i}`}
                        className="relative h-20 w-20 overflow-hidden border border-rule bg-paper-2"
                      >
                        <img
                          src={url}
                          alt={names[i] || `${field.label} ${i + 1}`}
                          className="h-full w-full object-cover"
                        />
                        <button
                          type="button"
                          className="absolute right-0 top-0 h-5 w-5 border border-rule bg-paper text-xs leading-none text-ink disabled:opacity-40"
                          aria-label={`移除第 ${i + 1} 张`}
                          // 上传中禁止删：删掉会让后面追加的图与预览位置错位
                          disabled={!!uploading[field.name]}
                          onClick={() => removeImageAt(field, i)}
                        >
                          ×
                        </button>
                      </div>
                    ))}
                  </div>
                ) : (
                  <div className="border border-dashed border-muted px-md py-lg text-center text-sm text-ink-2">
                    {uploading[field.name] ? '上传中…' : `还没有选择${field.label}`}
                  </div>
                )}
                <div className="mt-xs flex flex-wrap items-center gap-sm">
                  {/* 上传中锁住入口：并发上传会让追加顺序与预览位置错位 */}
                  <label
                    htmlFor={`upload-${field.name}`}
                    className={`btn-quiet cursor-pointer ${uploading[field.name] ? 'pointer-events-none opacity-50' : ''}`}
                  >
                    添加图片
                  </label>
                  <button
                    type="button"
                    className="btn-quiet"
                    disabled={!!uploading[field.name]}
                    onClick={() => setPickerFor(field)}
                  >
                    从素材库
                  </button>
                  <span className="text-xs text-muted">
                    {uploading[field.name]
                      ? '上传中…'
                      : urls.length > 0
                        ? `已选 ${urls.length} 张`
                        : '未选择'}
                  </span>
                </div>
              </div>
            ) : preview ? (
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
                        clearPreviews(field.name)
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

      {/* 素材选择弹窗（fixed 遮罩，不影响表单布局）。
          只看字段声明的种类；列表字段进多选模式 */}
      {pickerFor && (
        <AssetPickerModal
          kind={pickerFor.accept ?? 'image'}
          multiple={!!pickerFor.multiple}
          onSelect={(assets) => handlePickAssets(pickerFor, assets)}
          onClose={() => setPickerFor(null)}
        />
      )}
    </form>
  )
}
