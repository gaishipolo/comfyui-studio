/** 素材库 — 画廊、搜索、标签筛选、批量下载、详情、共享开关
 *
 * 用户隔离（默认隔离 + 手动共享）：列表只会出现「自己的 + 共享给团队的」
 * （admin 多见一份全部）。共享的素材别人只能看/下载，打标签、删除、
 * 共享开关仍是作者或管理员的权利 —— 界面上直接把没有权限的操作藏掉。 */
import { useState, useEffect, useCallback, useRef } from 'react'
import type { ChangeEvent } from 'react'
import type { Asset, Template, AssetTag, User } from '../types'
import {
  listAssets, listAssetTags, addAssetTag, removeAssetTag, shareAsset,
  prepareBatchDownload, getAssetUrl, getAssetThumbUrl, downloadAsset, deleteAsset, listUsers,
  uploadAssets,
} from '../api/client'
import { reportError, subscribeAssetsChanged } from '../notify'
import { alertDialog, confirmDialog } from './dialog'
import AuthMedia from './AuthMedia'
import { assetListViewState, createLatestRequestGate } from './listViewState'
import { useModalKeyboard } from './useModalKeyboard'


interface Props {
  templates: Template[]
  user: User
}

const formatSize = (bytes: number) =>
  bytes > 1024 * 1024
    ? `${(bytes / 1024 / 1024).toFixed(1)} MB`
    : `${(bytes / 1024).toFixed(0)} KB`

/** 文件大类分栏。'' = 全部；其余按 mime 前缀过滤，由后端做，不拉回来再筛 */
type KindFilter = '' | 'image' | 'video' | 'audio'
const KIND_TABS: { value: KindFilter; label: string }[] = [
  { value: '', label: '全部' },
  { value: 'image', label: '图片' },
  { value: 'video', label: '视频' },
  { value: 'audio', label: '音频' },
]

/** 模板分类里常出现 image/video/audio 这类「文件种类」值，和上面的类型分栏撞脸。
 *  标签栏只保留真正的业务分类，这几个由类型分栏统一管。 */
const FILE_KIND_TAGS = new Set(['image', 'video', 'audio'])

/** 按 MIME 大类给素材一个统一的中文格式角标（图片/视频/音频）。
 *  三类都给一枚小角标，保持一致；标签里的 image/video/audio 已被过滤，这里补格式标识不重复。 */
const formatLabel = (mime: string): string | null => {
  if (mime.startsWith('video/')) return '视频'
  if (mime.startsWith('audio/')) return '音频'
  if (mime.startsWith('image/')) return '图片'
  return null
}

export default function AssetGallery({ templates, user }: Props) {
  const [assets, setAssets] = useState<Asset[]>([])
  const assetRequests = useRef(createLatestRequestGate())

  // 归属判定：素材归属走 task.created_by（后端回填），显示名走 creator_name
  const isMine = (a: Asset) => !!a.created_by && a.created_by === user.id
  const canModify = (a: Asset) => user.role === 'admin' || isMine(a)
  const [tags, setTags] = useState<AssetTag[]>([])
  const [keyword, setKeyword] = useState('')
  const [debouncedKeyword, setDebouncedKeyword] = useState('')
  const [activeTag, setActiveTag] = useState<string | null>(null)
  const [activeTemplate, setActiveTemplate] = useState<string | null>(null)
  /** 文件大类分栏：素材不只有图片了（视频/音频也落库），列表得能分开看 */
  const [kindFilter, setKindFilter] = useState<KindFilter>('')
  /** 时间范围 + 创建人筛选（P1-3）。创建人下拉仅 admin —— 普通用户本来就只见自己的 */
  const [createdFrom, setCreatedFrom] = useState('')
  const [createdTo, setCreatedTo] = useState('')
  const [creatorFilter, setCreatorFilter] = useState('')
  const [allUsers, setAllUsers] = useState<User[]>([])
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [detail, setDetail] = useState<Asset | null>(null)
  const [tagDraft, setTagDraft] = useState('')
  const detailDialogRef = useRef<HTMLDivElement>(null)
  const closeDetail = useCallback(() => setDetail(null), [])
  useModalKeyboard(Boolean(detail), detailDialogRef, closeDetail)
  const [loading, setLoading] = useState(false)
  /** 加载失败标记：与「真的没有素材」区分开，别把网络错误渲染成空态 */
  const [loadFailed, setLoadFailed] = useState(false)
  /** 服务端准备批量归档需要时间；忙碌态同时阻止重复提交。 */
  const [downloading, setDownloading] = useState(false)
  const [deleting, setDeleting] = useState(false)
  /** 异步确认框期间的同步重入守卫（state 要等下一次渲染才生效） */
  const batchDeleteBusy = useRef(false)
  /** 上传态：百分比来自 XHR 的 upload.onprogress（视频可达 200MB，必须给反馈） */
  const [uploading, setUploading] = useState(false)
  const [uploadPercent, setUploadPercent] = useState(0)
  const fileInputRef = useRef<HTMLInputElement>(null)

  // 搜索防抖 300ms
  useEffect(() => {
    const timer = setTimeout(() => setDebouncedKeyword(keyword.trim()), 300)
    return () => clearTimeout(timer)
  }, [keyword])

  // 筛选条件一变就清空选中 —— 免得「删除 (N)」里混着看不见的图
  useEffect(() => {
    setSelected(new Set())
  }, [debouncedKeyword, activeTag, activeTemplate, createdFrom, createdTo, creatorFilter, kindFilter])

  useEffect(() => {
    setTagDraft('')
  }, [detail?.id])


  // 创建人下拉的数据源（admin 才有 —— 普通用户只见自己的，选人没意义）
  useEffect(() => {
    if (user.role !== 'admin') return
    listUsers()
      .then((data) => setAllUsers(Array.isArray(data) ? data : []))
      .catch(() => setAllUsers([]))
  }, [user.role])

  const fetchAssets = useCallback(async () => {
    const request = assetRequests.current.start()
    setLoading(true)
    try {
      const params: Record<string, string> = { limit: '100' }
      if (debouncedKeyword) params.keyword = debouncedKeyword
      if (activeTag) params.tag = activeTag
      if (activeTemplate) params.template_id = activeTemplate
      if (createdFrom) params.created_from = createdFrom
      if (createdTo) params.created_to = createdTo
      if (creatorFilter) params.creator = creatorFilter
      if (kindFilter) params.kind = kindFilter

      const data = await listAssets(params)
      if (!assetRequests.current.isLatest(request)) return
      setAssets(Array.isArray(data) ? data : [])
      setLoadFailed(false)
    } catch {
      if (!assetRequests.current.isLatest(request)) return
      setAssets([])
      setLoadFailed(true)
    } finally {
      if (assetRequests.current.isLatest(request)) setLoading(false)
    }
  }, [debouncedKeyword, activeTag, activeTemplate, createdFrom, createdTo, creatorFilter, kindFilter])

  const fetchTags = useCallback(async () => {
    try {
      const data = await listAssetTags()
      setTags(Array.isArray(data) ? data : [])
    } catch {
      // ignore
    }
  }, [])

  useEffect(() => {
    fetchAssets()
    fetchTags()
  }, [fetchAssets, fetchTags])
  // 任务跑完后素材库自动刷新 —— App 层全局任务轮询发的事件，不用切页面才看得见新素材
  useEffect(() => {
    return subscribeAssetsChanged(() => {
      fetchAssets()
      fetchTags()
    })
  }, [fetchAssets, fetchTags])

  const toggleSelect = (id: string) => {
    setSelected((prev) => {
      const next = new Set(prev)
      next.has(id) ? next.delete(id) : next.add(id)
      return next
    })
  }

  /** 全选当前筛选结果（不是库里全部） */
  const allVisibleSelected = assets.length > 0 && assets.every((a) => selected.has(a.id))
  const toggleSelectAll = () => {
    setSelected(allVisibleSelected ? new Set() : new Set(assets.map((a) => a.id)))
  }

  /** 把本地文件直接存进素材库。上传完刷新列表与标签（新素材可能带新标签）。 */
  const handleUpload = async (event: ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(event.target.files ?? [])
    // 清空 input：否则连续选同一个文件不会触发 change
    event.target.value = ''
    if (files.length === 0 || uploading) return

    setUploading(true)
    setUploadPercent(0)
    try {
      await uploadAssets(files, setUploadPercent)
      await Promise.all([fetchAssets(), fetchTags()])
    } catch (err) {
      reportError('上传失败', err)
    } finally {
      setUploading(false)
      setUploadPercent(0)
    }
  }

  const handleBatchDownload = async () => {    if (selected.size === 0 || downloading) return
    setDownloading(true)
    try {
      const url = await prepareBatchDownload([...selected])
      // 浏览器原生下载：ZIP 由下载管理器流式落盘，不经过页面内存 Blob。
      const a = document.createElement('a')
      a.href = url
      a.download = `assets_${Date.now()}.zip`
      document.body.appendChild(a)
      a.click()
      a.remove()
    } catch (err) {
      reportError('下载失败', err)
    } finally {
      setDownloading(false)
    }
  }

  /** 删除单个素材（详情面板）。不可逆，会连磁盘文件一起删 */
  const handleDeleteAsset = async (asset: Asset) => {
    const ok = await confirmDialog({
      title: '删除这个素材？',
      message: `文件：${asset.file_name}\n\n此操作不可撤销，文件会一并从磁盘删除。`,
      tone: 'danger',
      confirmText: '删除',
    })
    if (!ok) return
    try {
      const res = await deleteAsset(asset.id)
      setDetail(null)
      // 单删的这张若正被选中，从选中集摘掉 —— 别让「删除 (N)」虚高
      setSelected((prev) => {
        if (!prev.has(asset.id)) return prev
        const next = new Set(prev)
        next.delete(asset.id)
        return next
      })
      fetchAssets()
      fetchTags()
      // 记录一定已删，但磁盘文件可能没清掉 —— 如实告知，别笼统说"已删除"
      if (!res.file_removed) {
        await alertDialog({
          title: '记录已删除，但磁盘文件未能清理',
          message: `不影响素材库的正常使用。\n原因：${res.file_error || '未知'}\n\n如需释放磁盘空间，请联系管理员处理。`,
          tone: 'error',
        })
      }
    } catch (err) {
      reportError('删除失败', err)
    }
  }

  /** 批量删除已选素材。不可逆，会连磁盘文件一起删 */
  const handleBatchDelete = async () => {
    // 确认框是异步的，弹着的时候按钮还没禁用 —— 没有这道同步守卫，
    // 连点两次会排两个确认框，用户各确认一次就把同一批删了两遍。
    if (selected.size === 0 || batchDeleteBusy.current) return
    batchDeleteBusy.current = true
    try {
      const ok = await confirmDialog({
        title: `删除选中的 ${selected.size} 个素材？`,
        message: '此操作不可撤销，文件会一并从磁盘删除。',
        tone: 'danger',
        confirmText: `删除 ${selected.size} 个`,
      })
      if (!ok) return
      setDeleting(true)
      const ids = [...selected]
      const failed: string[] = []
      const fileLeft: string[] = []
      for (const id of ids) {
        try {
          const res = await deleteAsset(id)
          if (!res.file_removed) fileLeft.push(`${id.slice(0, 8)}: ${res.file_error || '未知'}`)
        } catch (err) {
          failed.push(err instanceof Error ? err.message : String(err))
        }
      }
      setSelected(new Set())
      setDetail(null)
      fetchAssets()
      fetchTags()
      const lines: string[] = []
      if (failed.length) {
        lines.push(`有 ${failed.length} 个删除失败（多为非本人提交，需管理员操作）：\n${failed.join('\n')}`)
      }
      if (fileLeft.length) {
        lines.push(`${fileLeft.length} 个记录已删除，但磁盘文件未能清理：\n${fileLeft.join('\n')}`)
      }
      if (lines.length) {
        await alertDialog({
          title: '部分素材未能完全删除',
          message: lines.join('\n\n'),
          tone: 'error',
        })
      }
    } finally {
      batchDeleteBusy.current = false
      setDeleting(false)
    }
  }

  const handleAddTag = async (assetId: string, tag: string) => {
    if (!tag.trim()) return
    try {
      await addAssetTag(assetId, tag.trim())
      fetchAssets()
      fetchTags()
    } catch (err) {
      reportError('添加标签失败', err)
    }
  }

  const handleRemoveTag = async (assetId: string, tag: string) => {
    try {
      await removeAssetTag(assetId, tag)
      fetchAssets()
      fetchTags()
    } catch (err) {
      reportError('移除标签失败', err)
    }
  }

  /** 共享开关：共享给团队 = 队友可见可下载，但他们改不了。仅本人或管理员 */
  const handleToggleShare = async (asset: Asset) => {
    try {
      await shareAsset(asset.id, !asset.is_shared)
      fetchAssets()
      setDetail((prev) =>
        prev && prev.id === asset.id ? { ...prev, is_shared: !asset.is_shared } : prev,
      )
    } catch (err) {
      reportError('操作失败', err)
    }
  }

  const assetUrl = (asset: Asset) => getAssetUrl(asset.id)

  const viewState = assetListViewState({
    loading,
    failed: loadFailed,
    count: assets.length,
  })

  return (
    <section>
      {/* 页头：标题 + 主操作。上传是这一页的主动作，放右上角与其他页对齐 */}
      <div className="page-head">
        <div>
          <h2 className="page-title">素材库</h2>
          <p className="page-sub">
            {assets.length === 0
              ? '生成产物与上传的文件都会收在这里'
              : `共 ${assets.length} 个素材`}
          </p>
        </div>
        <div className="flex items-center gap-xs">
          <button
            type="button"
            onClick={() => fileInputRef.current?.click()}
            className="btn-primary"
            disabled={uploading}
          >
            {uploading ? `上传中 ${uploadPercent}%` : '上传素材'}
          </button>
          <input
            ref={fileInputRef}
            type="file"
            multiple
            accept="image/*,audio/*,video/*"
            className="hidden"
            onChange={handleUpload}
          />
          {assets.length > 0 && (
            <button onClick={toggleSelectAll} className="btn">
              {allVisibleSelected ? '取消全选' : `全选 (${assets.length})`}
            </button>
          )}
        </div>
      </div>

      {/* 筛选栏：第一行是搜索与范围，第二行是分类 chips —— 控件不再挤成一长条 */}
      <div className="panel mb-md p-md">
        <div className="flex flex-wrap items-center gap-xs">
          <input
            type="text"
            placeholder="搜索素材（文件名/参数）"
            aria-label="搜索素材"
            className="field min-w-[12rem] max-w-[22rem] flex-1"
            value={keyword}
            onChange={(e) => setKeyword(e.target.value)}
          />
          <select
            className="field w-[9.5rem]"
            aria-label="按生成场景筛选"
            value={activeTemplate ?? ''}
            onChange={(e) => setActiveTemplate(e.target.value || null)}
          >
            <option value="">全部场景</option>
            {templates.map((t) => (
              <option key={t.id} value={t.id}>
                {t.name}
              </option>
            ))}
          </select>
          {user.role === 'admin' && (
            <select
              className="field w-[8.5rem]"
              aria-label="按创建人筛选"
              value={creatorFilter}
              onChange={(e) => setCreatorFilter(e.target.value)}
            >
              <option value="">全部创建人</option>
              {allUsers.map((u) => (
                <option key={u.id} value={u.id}>
                  {u.display_name || u.username}
                </option>
              ))}
            </select>
          )}
          <span className="ml-2xs flex items-center gap-xs">
            <input
              type="date"
              className="field w-[9.5rem]"
              aria-label="创建时间起"
              title="创建时间起（含当天）"
              value={createdFrom}
              onChange={(e) => setCreatedFrom(e.target.value)}
            />
            <span className="text-sm text-muted">至</span>
            <input
              type="date"
              className="field w-[9.5rem]"
              aria-label="创建时间止"
              title="创建时间止（含当天）"
              value={createdTo}
              onChange={(e) => setCreatedTo(e.target.value)}
            />
          </span>
        </div>

        {/* 分类行：先按类型（真实 MIME）分大类，再按标签细分，同一行并排 —— 
            两行各占 40px 纯属浪费，中间用竖线隔开就够区分。
            标签里过滤掉 image/video/audio 这类「文件种类」值，它们由类型栏统一管。 */}
        <div className="mt-sm flex flex-wrap items-center gap-xs border-t border-rule pt-sm">
          <span className="mr-2xs text-xs font-medium text-muted">类型</span>
          {KIND_TABS.map((tab) => (
            <button
              key={tab.value || 'all'}
              type="button"
              onClick={() => setKindFilter(tab.value)}
              className="chip cursor-pointer"
              data-state={kindFilter === tab.value ? 'on' : undefined}
              aria-pressed={kindFilter === tab.value}
            >
              {tab.label}
            </button>
          ))}
          {tags.filter((t) => !FILE_KIND_TAGS.has(t.name)).length > 0 && (
            <>
              <span aria-hidden="true" className="mx-2xs h-4 w-px bg-rule" />
              <span className="mr-2xs text-xs font-medium text-muted">标签</span>
              <button
                onClick={() => setActiveTag(null)}
                className="chip cursor-pointer"
                data-state={!activeTag ? 'on' : undefined}
              >
                全部
              </button>
              {tags
                .filter((t) => !FILE_KIND_TAGS.has(t.name))
                .map((tag) => (
                  <button
                    key={tag.name}
                    onClick={() => setActiveTag(activeTag === tag.name ? null : tag.name)}
                    className="chip cursor-pointer"
                    data-state={activeTag === tag.name ? 'on' : undefined}
                  >
                    {tag.name} ({tag.count})
                  </button>
                ))}
            </>
          )}
        </div>
      </div>

      {/* 批量操作条：独立成条，避免勾选时整行筛选控件重排 */}
      {selected.size > 0 && (
        <div className="bulk-bar">
          <span className="bulk-bar-count">已选 {selected.size} 项</span>
          <button
            onClick={handleBatchDownload}
            className="btn btn-primary"
            disabled={downloading}
          >
            {downloading ? '打包中…' : `下载 (${selected.size})`}
          </button>
          <button onClick={handleBatchDelete} className="btn btn-danger" disabled={deleting}>
            {deleting ? '删除中…' : `删除 (${selected.size})`}
          </button>
          <button onClick={() => setSelected(new Set())} className="btn-quiet">
            清除选择
          </button>
        </div>
      )}

      {/* 素材画廊 */}
      {viewState === 'loading' ? (
        <div
          className="grid grid-cols-2 gap-md sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5"
          aria-hidden="true"
        >
          {Array.from({ length: 10 }).map((_, i) => (
            <div key={i} className="card">
              <div className="skeleton aspect-square" />
              <div className="p-sm">
                <div className="skeleton h-2xs w-2/3" />
                <div className="skeleton mt-xs h-2xs w-1/3" />
              </div>
            </div>
          ))}
        </div>
      ) : viewState === 'error' ? (
        <div className="flex min-h-[12rem] flex-col items-center justify-center gap-sm rounded-md border border-dashed border-error-line bg-error-soft text-base text-error">
          <span>素材加载失败，请检查网络后重试</span>
          <button onClick={() => fetchAssets()} className="btn">
            重试
          </button>
        </div>
      ) : viewState === 'empty' ? (
        <div className="empty">
          <span className="empty-icon" aria-hidden="true">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none">
              <path
                d="M4 5h16v14H4zM9.5 10.5a1.5 1.5 0 1 0 0-3 1.5 1.5 0 0 0 0 3M20 16l-4.5-4.5L7 20"
                stroke="currentColor"
                strokeWidth="1.7"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
          </span>
          <p className="empty-title">暂无素材</p>
          <p className="empty-hint">去生成页面创建内容，或点「上传素材」直接导入</p>
        </div>
      ) : (
        <div className="grid grid-cols-2 gap-md sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5">
          {assets.map((asset) => (
            <div key={asset.id} className="card card-hover group relative">
              <button
                type="button"
                aria-pressed={selected.has(asset.id)}
                aria-label={`选择素材 ${asset.file_name}`}
                onClick={(e) => {
                  e.stopPropagation()
                  toggleSelect(asset.id)
                }}
                className="select-toggle left-xs top-xs"
              >
                {selected.has(asset.id) && '✓'}
              </button>

              {/* 归属/共享角标：别人的共享素材标来源，自己的共享标状态；
                  admin 看别人的私有素材也标来源 */}
              {((asset.is_shared && asset.creator_name) || (!isMine(asset) && asset.creator_name)) && (
                <span className="pointer-events-none absolute right-xs top-xs z-raised rounded-pill border border-rule bg-[rgba(255,255,255,0.92)] px-xs py-2xs text-xs text-ink-2 shadow-xs backdrop-blur-sm">
                  {asset.is_shared && isMine(asset)
                    ? '共享中'
                    : asset.is_shared
                      ? `来自 ${asset.creator_name}`
                      : asset.creator_name}
                </span>
              )}

              <button
                type="button"
                onClick={() => setDetail(asset)}
                className="block w-full text-left"
              >
                <span className="relative block aspect-square overflow-hidden bg-paper-2">
                  {(asset.mime_type || '').startsWith('audio/') ? (
                    // 音频没有画面，也不值得为了一个格子把整段音频拉下来
                    <span className="flex h-full w-full items-center justify-center px-xs text-center text-xs text-muted">
                      ♪ {asset.file_name}
                    </span>
                  ) : (
                    <AuthMedia
                      src={assetUrl(asset)}
                      posterSrc={getAssetThumbUrl(asset.id)}
                      alt={asset.file_name}
                      mime={asset.mime_type}
                      className="h-full w-full object-cover transition-transform duration-short ease-out group-hover:scale-[1.02]"
                      loading="lazy"
                    />
                  )}

                  {/* 格式角标：图片/视频/音频统一一枚小角标，按 MIME 判定。
                      标签里的 image/video/audio 已过滤，这里补一个统一的格式标识，三类一致、不重复。 */}
                  {formatLabel(asset.mime_type || '') && (
                    <span className="pointer-events-none absolute bottom-xs left-xs rounded-pill border border-rule bg-[rgba(255,255,255,0.92)] px-xs py-2xs text-xs text-ink-2 shadow-xs backdrop-blur-sm">
                      {formatLabel(asset.mime_type || '')}
                    </span>
                  )}
                </span>
                <span className="block p-sm">
                  <span className="flex flex-wrap gap-2xs">
                    {(asset.tags || [])
                      .filter((tag) => !FILE_KIND_TAGS.has(tag))
                      .slice(0, 3)
                      .map((tag) => (
                      <span
                        key={tag}
                        className="rounded-pill border border-rule bg-paper-2 px-xs text-xs leading-5 text-ink-2"
                      >
                        {tag}
                      </span>
                    ))}
                  </span>
                  <span
                    className="mt-2xs block truncate text-xs font-medium text-ink-2"
                    title={asset.file_name}
                  >
                    {asset.file_name}
                  </span>
                  <span className="mt-2xs block truncate text-xs text-muted">
                    {new Date(asset.created_at).toLocaleDateString('zh-CN')}
                    {asset.file_size ? ` · ${formatSize(asset.file_size)}` : ''}
                  </span>
                </span>
              </button>
            </div>
          ))}
        </div>
      )}

      {/* 详情面板 */}
      {detail && (
        <div
          className="fixed inset-0 z-modal flex items-center justify-center bg-scrim p-md"
          onClick={() => setDetail(null)}
        >
          <div
            ref={detailDialogRef}
            role="dialog"
            aria-modal="true"
            aria-labelledby="asset-detail-title"
            tabIndex={-1}
            className="flex max-h-[90dvh] w-full max-w-4xl overflow-hidden rounded-lg border border-rule bg-paper shadow-lg"
            onClick={(e) => e.stopPropagation()}
          >
            {/* 左侧预览：图片/视频/音频按 MIME 分流 */}
            <div className="flex-1 bg-paper-2">
              <AuthMedia
                src={assetUrl(detail)}
                alt={detail.file_name}
                mime={detail.mime_type}
                controls
                className="h-full w-full object-contain"
              />
            </div>

            {/* 右侧信息 */}
            <div className="w-80 shrink-0 overflow-y-auto border-l border-rule p-lg">
              <div className="flex items-center justify-between gap-xs">
                <h3 id="asset-detail-title" className="font-display text-title">素材详情</h3>
                <button
                  onClick={() => setDetail(null)}
                  className="x-btn"
                  aria-label="关闭素材详情"
                >
                  ✕
                </button>
              </div>

              <dl className="mt-md text-sm">
                <div className="flex items-baseline justify-between gap-sm border-b border-rule py-xs">
                  <dt className="shrink-0 text-xs text-muted">文件</dt>
                  <dd className="min-w-0 break-all text-right text-ink">{detail.file_name}</dd>
                </div>
                <div className="flex items-baseline justify-between gap-sm border-b border-rule py-xs">
                  <dt className="shrink-0 text-xs text-muted">大小</dt>
                  <dd className="num text-right text-ink">{formatSize(detail.file_size)}</dd>
                </div>
                <div className="flex items-baseline justify-between gap-sm border-b border-rule py-xs">
                  <dt className="shrink-0 text-xs text-muted">生成时间</dt>
                  <dd className="num text-right text-ink">
                    {new Date(detail.created_at).toLocaleString('zh-CN')}
                  </dd>
                </div>
                <div className="flex items-baseline justify-between gap-sm border-b border-rule py-xs">
                  <dt className="shrink-0 text-xs text-muted">归属</dt>
                  <dd className="flex flex-wrap items-center justify-end gap-2xs text-right text-ink">
                    {isMine(detail) ? '我' : detail.creator_name || '无主数据'}
                    {detail.is_shared && (
                      <span className="chip" data-tone="accent">
                        已共享
                      </span>
                    )}
                  </dd>
                </div>
              </dl>

              {/* 共享开关（仅本人/管理员）：共享的是「看」的权限，不是共同所有 */}
              {canModify(detail) && (
                <div className="mt-md rounded-sm border border-rule bg-paper-2 p-sm">
                  <div className="text-xs leading-relaxed text-muted">
                    {detail.is_shared
                      ? '当前共享给团队：队友可见可下载，但不能改'
                      : '当前仅自己可见（admin 不受此限制）'}
                  </div>
                  <button
                    type="button"
                    onClick={() => handleToggleShare(detail)}
                    className="btn mt-xs"
                  >
                    {detail.is_shared ? '取消共享' : '共享给团队'}
                  </button>
                </div>
              )}

              {/* 标签管理：别人的共享素材只能看 —— 打标签是作者/管理员的权利 */}
              <div className="mt-md">
                <div className="text-xs text-muted">标签</div>
                <div className="mt-2xs flex flex-wrap gap-2xs">
                  {(detail.tags || [])
                    .filter((tag) => !FILE_KIND_TAGS.has(tag))
                    .map((tag) => (
                    <span key={tag} className="chip">
                      {tag}
                      {canModify(detail) && (
                        <button
                          onClick={() => handleRemoveTag(detail.id, tag)}
                          className="cursor-pointer text-muted transition-colors hover:text-error"
                          aria-label={`移除标签 ${tag}`}
                        >
                          ✕
                        </button>
                      )}
                    </span>
                  ))}
                </div>
                {canModify(detail) && (
                  <input
                    type="text"
                    placeholder="+ 添加标签，回车确认"
                    aria-label="添加标签"
                    className="field mt-xs text-sm"
                    value={tagDraft}
                    onChange={(e) => setTagDraft(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter') {
                        handleAddTag(detail.id, tagDraft)
                        setTagDraft('')
                      }
                    }}
                  />
                )}
              </div>

              {/* 生成参数 */}
              {detail.meta && Object.keys(detail.meta).length > 0 && (
                <div className="mt-md">
                  <div className="text-xs text-muted">生成参数</div>
                  <dl className="mt-2xs rounded-sm border border-rule bg-paper-2 p-sm">
                    {Object.entries(detail.meta).map(([key, value]) => (
                      <div key={key} className="text-xs">
                        <dt className="inline text-muted">{key}: </dt>
                        <dd className="inline text-ink">{String(value).slice(0, 80)}</dd>
                      </div>
                    ))}
                  </dl>
                </div>
              )}

              {/* 操作 */}
              <div className="mt-md flex gap-xs border-t border-rule pt-md">
                <button
                  type="button"
                  onClick={() => {
                    downloadAsset(assetUrl(detail), detail.file_name).catch((err) =>
                      reportError('下载失败', err),
                    )
                  }}
                  className="btn btn-primary flex-1 justify-center"
                >
                  下载
                </button>
                {canModify(detail) && (
                  <button
                    type="button"
                    onClick={() => handleDeleteAsset(detail)}
                    className="btn btn-danger"
                  >
                    删除
                  </button>
                )}
              </div>
            </div>
          </div>
        </div>
      )}
    </section>
  )
}
