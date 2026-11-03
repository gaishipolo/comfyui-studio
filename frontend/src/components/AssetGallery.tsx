/** 素材库 — 画廊、搜索、标签筛选、批量下载、详情、共享开关
 *
 * 用户隔离（默认隔离 + 手动共享）：列表只会出现「自己的 + 共享给团队的」
 * （admin 多见一份全部）。共享的素材别人只能看/下载，打标签、删除、
 * 共享开关仍是作者或管理员的权利 —— 界面上直接把没有权限的操作藏掉。 */
import { useState, useEffect, useCallback, useRef } from 'react'
import type { ChangeEvent } from 'react'
import type { Asset, Template, AssetTag, User, StorageQuota } from '../types'
import {
  listAssets, listAssetTags, addAssetTag, removeAssetTag, shareAsset,
  prepareBatchDownload, getAssetUrl, getAssetThumbUrl, downloadAsset, deleteAsset, listUsers,
  uploadAssets, listTrash, restoreAsset, purgeAsset, emptyTrash, getQuota,
} from '../api/client'
import { reportError, subscribeAssetsChanged } from '../notify'
import { formatDate, formatDateTime } from '../time'
import { alertDialog, confirmDialog } from './dialog'
import AuthMedia from './AuthMedia'
import { assetListViewState, createLatestRequestGate } from './listViewState'
import { useModalKeyboard } from './useModalKeyboard'


interface Props {
  templates: Template[]
  user: User
}

const formatSize = (bytes: number) => {
  if (!bytes) return '0 B'
  if (bytes >= 1024 ** 3) return `${(bytes / 1024 ** 3).toFixed(2)} GB`
  if (bytes >= 1024 ** 2) return `${(bytes / 1024 / 1024).toFixed(1)} MB`
  if (bytes >= 1024) return `${(bytes / 1024).toFixed(0)} KB`
  return `${bytes} B`
}

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

/** 按 MIME 大类给素材一枚中文格式角标。
 *  只标视频/音频 —— 素材库里绝大多数是图片，每张都盖一个「图片」等于没有信息，
 *  还会压住画面左上/右上的内容；真正需要提示的是「这块不是静态图」。 */
const formatLabel = (mime: string): string | null => {
  if (mime.startsWith('video/')) return '视频'
  if (mime.startsWith('audio/')) return '音频'
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

  /** 素材库 / 回收站两个视图。回收站独立拉数据，因为后端把软删除素材
   *  从正常列表里彻底排除了 —— 不单独请求就等于「删了就再也看不见」 */
  const [view, setView] = useState<'library' | 'trash'>('library')
  const [trash, setTrash] = useState<Asset[]>([])
  const [trashLoading, setTrashLoading] = useState(false)
  const [trashBusy, setTrashBusy] = useState(false)
  /** 存储配额（含回收站占用）。上传超额时后端 403，这里提前展示并置灰按钮 */
  const [quota, setQuota] = useState<StorageQuota | null>(null)

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
      // 别处删掉的素材还在 selected 里的话，批量删除会对已消失的 id 逐个 404
      setSelected((prev) => {
        if (prev.size === 0) return prev
        const alive = new Set((Array.isArray(data) ? data : []).map((a) => a.id))
        const next = new Set([...prev].filter((id) => alive.has(id)))
        return next.size === prev.size ? prev : next
      })
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

  /** 配额概览。任何改变占用大小的操作之后都要重拉 —— 回收站里的文件仍占空间，
   *  「移入回收站」不会让这个数字变小，只有「彻底删除」才会。 */
  const fetchQuota = useCallback(async () => {
    try {
      setQuota(await getQuota())
    } catch {
      // 配额拿不到不影响浏览：静默降级成"不展示配额条"
      setQuota(null)
    }
  }, [])

  const fetchTrash = useCallback(async () => {
    setTrashLoading(true)
    try {
      const data = await listTrash(200)
      setTrash(Array.isArray(data) ? data : [])
    } catch (err) {
      reportError('回收站加载失败', err)
      setTrash([])
    } finally {
      setTrashLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchAssets()
    fetchTags()
    fetchQuota()
    // 进页面就拉一次回收站：一是让「回收站 (N)」有数，二是删完东西后
    // 用户能立刻看到"东西还在、可以找回"，而不是以为永久没了。
    fetchTrash()
  }, [fetchAssets, fetchTags, fetchQuota, fetchTrash])
  // 任务跑完后素材库自动刷新 —— App 层全局任务轮询发的事件，不用切页面才看得见新素材
  useEffect(() => {
    return subscribeAssetsChanged(() => {
      fetchAssets()
      fetchTags()
      fetchQuota()
    })
  }, [fetchAssets, fetchTags, fetchQuota])

  useEffect(() => {
    if (view === 'trash') fetchTrash()
  }, [view, fetchTrash])

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
      await Promise.all([fetchAssets(), fetchTags(), fetchQuota()])
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

  /** 删除单个素材（详情面板）→ **移入回收站**，可恢复；空间不释放 */
  const handleDeleteAsset = async (asset: Asset) => {
    const ok = await confirmDialog({
      title: '移入回收站？',
      message: `文件：${asset.file_name}\n\n可以在「回收站」里恢复。空间不会立即释放 —— 需要彻底删除才会腾出配额。`,
      tone: 'danger',
      confirmText: '移入回收站',
    })
    if (!ok) return
    try {
      await deleteAsset(asset.id)
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
      fetchQuota()
    } catch (err) {
      reportError('删除失败', err)
    }
  }

  /** 批量删除已选素材 → 全部移入回收站 */
  const handleBatchDelete = async () => {
    // 确认框是异步的，弹着的时候按钮还没禁用 —— 没有这道同步守卫，
    // 连点两次会排两个确认框，用户各确认一次就把同一批删了两遍。
    if (selected.size === 0 || batchDeleteBusy.current) return
    batchDeleteBusy.current = true
    try {
      const ok = await confirmDialog({
        title: `将选中的 ${selected.size} 个素材移入回收站？`,
        message: '可以在「回收站」里逐个恢复。空间不会立即释放 —— 需要彻底删除才会腾出配额。',
        tone: 'danger',
        confirmText: `移入回收站（${selected.size}）`,
      })
      if (!ok) return
      setDeleting(true)
      const ids = [...selected]
      const failed: string[] = []
      for (const id of ids) {
        try {
          await deleteAsset(id)
        } catch (err) {
          failed.push(err instanceof Error ? err.message : String(err))
        }
      }
      setSelected(new Set())
      setDetail(null)
      fetchAssets()
      fetchTags()
      fetchQuota()
      if (failed.length) {
        await alertDialog({
          title: '部分素材未能移入回收站',
          message: `有 ${failed.length} 个删除失败（多为非本人提交，需管理员操作）：\n${failed.join('\n')}`,
          tone: 'error',
        })
      }
    } finally {
      batchDeleteBusy.current = false
      setDeleting(false)
    }
  }

  /** 从回收站恢复 */
  const handleRestore = async (asset: Asset) => {
    if (trashBusy) return
    setTrashBusy(true)
    try {
      await restoreAsset(asset.id)
      await Promise.all([fetchTrash(), fetchAssets(), fetchTags(), fetchQuota()])
    } catch (err) {
      reportError('恢复失败', err)
    } finally {
      setTrashBusy(false)
    }
  }

  /** 彻底删除单个素材（记录 + 磁盘文件）。这是唯一真正释放配额的操作 */
  const handlePurge = async (asset: Asset) => {
    if (trashBusy) return
    const ok = await confirmDialog({
      title: '彻底删除这个素材？',
      message: `文件：${asset.file_name}\n\n记录和磁盘文件都会被删除，无法恢复，但会释放它占用的空间。`,
      tone: 'danger',
      confirmText: '彻底删除',
    })
    if (!ok) return
    setTrashBusy(true)
    try {
      const res = await purgeAsset(asset.id)
      await Promise.all([fetchTrash(), fetchQuota()])
      // 记录一定已删，但磁盘文件可能没清掉 —— 如实告知，别笼统说"已删除"
      if (!res.file_removed) {
        await alertDialog({
          title: '记录已删除，但磁盘文件未能清理',
          message: `原因：${res.file_error || '未知'}\n\n如需释放磁盘空间，请联系管理员处理。`,
          tone: 'error',
        })
      }
    } catch (err) {
      reportError('彻底删除失败', err)
    } finally {
      setTrashBusy(false)
    }
  }

  /** 清空回收站。后端分批（每批 50），没清完会回 remaining>0 */
  const handleEmptyTrash = async () => {
    if (trashBusy || trash.length === 0) return
    const ok = await confirmDialog({
      title: `彻底删除回收站里的 ${trash.length} 个素材？`,
      message: '这些记录和磁盘文件都会被删除，无法恢复。',
      tone: 'danger',
      confirmText: '全部彻底删除',
    })
    if (!ok) return
    setTrashBusy(true)
    try {
      const res = await emptyTrash()
      await Promise.all([fetchTrash(), fetchQuota()])
      const lines: string[] = [`已彻底删除 ${res.deleted} 个素材。`]
      if (res.remaining > 0) {
        lines.push(`还有 ${res.remaining} 个未清理（单次有上限），请再执行一次。`)
      }
      if (res.file_errors.length) {
        lines.push(`有 ${res.file_errors.length} 个磁盘文件未能清理：\n${res.file_errors.join('\n')}`)
      }
      await alertDialog({
        title: res.file_errors.length ? '部分文件未能清理' : '回收站已清空',
        message: lines.join('\n\n'),
        tone: res.file_errors.length ? 'error' : 'success',
      })
    } catch (err) {
      reportError('清空回收站失败', err)
    } finally {
      setTrashBusy(false)
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

  /** 配额用尽：上传和提交任务都会被后端 403，界面提前置灰并说明原因 */
  const quotaFull = !!quota && !quota.unlimited && quota.used_bytes >= quota.quota_bytes
  /** 不限配额时「已用」只是一句数字，收进副标题就够 —— 原来它独占一整条卡片，
   *  视觉上像一根永远空着的进度条，还白占 80px 版面 */
  const quotaInline =
    quota?.unlimited && quota.used_bytes > 0 ? ` · 已用 ${formatSize(quota.used_bytes)}` : ''

  return (
    <section>
      {/* 页头：标题 + 主操作。上传是这一页的主动作，放右上角与其他页对齐 */}
      <div className="page-head">
        <div>
          <h2 className="page-title">素材库</h2>
          <p className="page-sub">
            {view === 'trash'
              ? trash.length === 0
                ? '回收站是空的'
                : `回收站里有 ${trash.length} 个素材，可恢复或彻底删除`
              : assets.length === 0
                ? '生成产物与上传的文件都会收在这里'
                : `共 ${assets.length} 个素材`}
            {quotaInline}
          </p>
        </div>
        <div className="flex items-center gap-xs">
          <button
            type="button"
            onClick={() => setView(view === 'trash' ? 'library' : 'trash')}
            className="btn"
            aria-pressed={view === 'trash'}
          >
            {view === 'trash' ? '返回素材库' : `回收站${trash.length > 0 ? ` (${trash.length})` : ''}`}
          </button>
          {view === 'library' && (
            <>
              <button
                type="button"
                onClick={() => fileInputRef.current?.click()}
                className="btn-primary"
                disabled={uploading || quotaFull}
                title={quotaFull ? '存储配额已满，请先彻底删除回收站里的素材' : undefined}
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
            </>
          )}
        </div>
      </div>

      {/* 配额条。注意「已用」包含回收站 —— 软删除不动磁盘文件，删了不会变小；
          只有彻底删除才释放。这也是为什么超限时的提示要指向回收站。
          不限配额时不渲染这张卡片（数字已并入副标题），省掉一整条空荡荡的版面。 */}
      {quota && !quota.unlimited && (
        <div className="panel mb-md px-md py-sm">
          <div className="flex flex-wrap items-baseline justify-between gap-xs text-xs">
            <span className="text-muted">
              已用 {formatSize(quota.used_bytes)} / {formatSize(quota.quota_bytes)}
            </span>
            <span className={quotaFull ? 'font-medium text-error' : 'text-muted'}>
              {quota.percent ?? 0}%
              {quotaFull ? ' · 已满，上传与提交任务会被拒绝' : ''}
            </span>
          </div>
          <div
            className="mt-2xs h-1.5 overflow-hidden rounded-pill bg-paper-2"
            role="progressbar"
            aria-label="存储配额使用率"
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={Math.min(100, quota.percent ?? 0)}
          >
            <div
              className={`h-full rounded-pill transition-[width] duration-short ${
                quotaFull ? 'bg-error' : 'bg-accent'
              }`}
              style={{ width: `${Math.min(100, Math.max(0, quota.percent ?? 0))}%` }}
            />
          </div>
        </div>
      )}

      {/* 筛选栏：第一行是搜索与范围，第二行是分类 chips —— 控件不再挤成一长条 */}
      {view === 'trash' ? (
        <div className="panel">
          <div className="flex flex-wrap items-center justify-between gap-sm border-b border-rule px-md py-sm">
            <div>
              <h3 className="text-title">回收站</h3>
              <p className="mt-2xs text-xs text-muted">
                这里的素材不出现在列表和统计里，但文件仍在磁盘上、仍占配额。
                恢复即可继续使用；「彻底删除」才会真正释放空间，且不可撤销。
              </p>
            </div>
            <button
              type="button"
              className="btn btn-danger"
              onClick={handleEmptyTrash}
              disabled={trashBusy || trash.length === 0}
            >
              {trashBusy ? '处理中…' : '清空回收站'}
            </button>
          </div>

          {trashLoading ? (
            <div className="p-md text-sm text-muted">加载中…</div>
          ) : trash.length === 0 ? (
            <div className="p-lg text-center text-sm text-muted">
              回收站是空的。删除的素材会先放到这里，随时可以恢复。
            </div>
          ) : (
            <ul>
              {trash.map((asset) => (
                <li
                  key={asset.id}
                  className="flex flex-wrap items-center justify-between gap-sm border-t border-rule px-md py-sm first:border-t-0"
                >
                  <div className="min-w-0">
                    <div className="truncate text-sm text-ink" title={asset.file_name}>
                      {asset.file_name}
                    </div>
                    <div className="mt-2xs text-xs text-muted">
                      {formatSize(asset.file_size)} · 删除于{' '}
                      {formatDateTime(asset.deleted_at ?? '')}
                      {!isMine(asset) && asset.creator_name
                        ? ` · 来自 ${asset.creator_name}`
                        : ''}
                    </div>
                  </div>
                  <div className="flex shrink-0 gap-xs">
                    <button
                      type="button"
                      className="btn"
                      onClick={() => handleRestore(asset)}
                      disabled={trashBusy}
                    >
                      恢复
                    </button>
                    <button
                      type="button"
                      className="btn btn-danger"
                      onClick={() => handlePurge(asset)}
                      disabled={trashBusy}
                    >
                      彻底删除
                    </button>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </div>
      ) : (
        <>
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
            {deleting ? '删除中…' : `移入回收站 (${selected.size})`}
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
                    {formatDate(asset.created_at)}
                    {asset.file_size ? ` · ${formatSize(asset.file_size)}` : ''}
                  </span>
                </span>
              </button>
            </div>
          ))}
        </div>
      )}
        </>
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
                    {formatDateTime(detail.created_at)}
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
