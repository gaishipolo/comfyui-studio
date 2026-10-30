/** 素材库 — 画廊、搜索、标签筛选、批量下载、详情、共享开关
 *
 * 用户隔离（默认隔离 + 手动共享）：列表只会出现「自己的 + 共享给团队的」
 * （admin 多见一份全部）。共享的素材别人只能看/下载，打标签、删除、
 * 共享开关仍是作者或管理员的权利 —— 界面上直接把没有权限的操作藏掉。 */
import { useState, useEffect, useCallback } from 'react'
import type { Asset, Template, AssetTag, User } from '../types'
import {
  listAssets, listAssetTags, addAssetTag, removeAssetTag, shareAsset,
  batchDownload, getAssetUrl, getAssetThumbUrl, downloadAsset, deleteAsset, listUsers,
} from '../api/client'
import { subscribeAssetsChanged } from '../notify'
import AuthMedia from './AuthMedia'

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

export default function AssetGallery({ templates, user }: Props) {
  const [assets, setAssets] = useState<Asset[]>([])

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
  const [loading, setLoading] = useState(false)

  // 搜索防抖 300ms
  useEffect(() => {
    const timer = setTimeout(() => setDebouncedKeyword(keyword.trim()), 300)
    return () => clearTimeout(timer)
  }, [keyword])

  // 筛选条件一变就清空选中 —— 免得「删除 (N)」里混着看不见的图
  useEffect(() => {
    setSelected(new Set())
  }, [debouncedKeyword, activeTag, activeTemplate, createdFrom, createdTo, creatorFilter, kindFilter])

  // 创建人下拉的数据源（admin 才有 —— 普通用户只见自己的，选人没意义）
  useEffect(() => {
    if (user.role !== 'admin') return
    listUsers()
      .then((data) => setAllUsers(Array.isArray(data) ? data : []))
      .catch(() => setAllUsers([]))
  }, [user.role])

  const fetchAssets = useCallback(async () => {
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
      setAssets(Array.isArray(data) ? data : [])
    } catch {
      setAssets([])
    } finally {
      setLoading(false)
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

  const handleBatchDownload = async () => {
    if (selected.size === 0) return
    try {
      const blob = await batchDownload([...selected])
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `assets_${Date.now()}.zip`
      a.click()
      URL.revokeObjectURL(url)
    } catch (err) {
      alert(`下载失败: ${err instanceof Error ? err.message : err}`)
    }
  }

  /** 删除单个素材（详情面板）。不可逆，会连磁盘文件一起删 */
  const handleDeleteAsset = async (asset: Asset) => {
    if (
      !confirm(
        `确定删除素材「${asset.file_name}」？\n\n此操作不可撤销，文件会一并从磁盘删除。`,
      )
    )
      return
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
        alert(
          `记录已删除，但磁盘文件未能清理（不影响素材库）。\n原因：${res.file_error || '未知'}\n\n如需释放磁盘空间，请联系管理员处理。`,
        )
      }
    } catch (err) {
      alert(`删除失败: ${err instanceof Error ? err.message : err}`)
    }
  }

  /** 批量删除已选素材。不可逆，会连磁盘文件一起删 */
  const handleBatchDelete = async () => {
    if (selected.size === 0) return
    if (
      !confirm(
        `确定删除选中的 ${selected.size} 个素材？\n\n此操作不可撤销，文件会一并从磁盘删除。`,
      )
    )
      return
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
    // 记录删除失败 ≠ 文件没清掉，两种情况分开说，别混成一句"失败"
    const lines: string[] = []
    if (failed.length) {
      lines.push(`有 ${failed.length} 个删除失败（多为非本人提交，需管理员操作）：\n${failed.join('\n')}`)
    }
    if (fileLeft.length) {
      lines.push(
        `${fileLeft.length} 个记录已删除，但磁盘文件未能清理：\n${fileLeft.join('\n')}`,
      )
    }
    if (lines.length) alert(lines.join('\n\n'))
  }

  const handleAddTag = async (assetId: string, tag: string) => {
    if (!tag.trim()) return
    try {
      await addAssetTag(assetId, tag.trim())
      fetchAssets()
      fetchTags()
    } catch (err) {
      alert(`添加标签失败: ${err instanceof Error ? err.message : err}`)
    }
  }

  const handleRemoveTag = async (assetId: string, tag: string) => {
    try {
      await removeAssetTag(assetId, tag)
      fetchAssets()
      fetchTags()
    } catch (err) {
      alert(`移除标签失败: ${err instanceof Error ? err.message : err}`)
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
      alert(`操作失败: ${err instanceof Error ? err.message : err}`)
    }
  }

  const assetUrl = (asset: Asset) => getAssetUrl(asset.task_id, asset.file_name)

  return (
    <section>
      {/* 搜索 + 筛选栏 */}
      <div className="rule-b pb-md">
        <div className="flex flex-wrap items-center gap-xs">
          <input
            type="text"
            placeholder="搜索素材（文件名/参数）"
            aria-label="搜索素材"
            className="field min-w-[12rem] flex-1"
            value={keyword}
            onChange={(e) => setKeyword(e.target.value)}
          />
          <select
            className="field w-auto"
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
          <input
            type="date"
            className="field w-auto"
            aria-label="创建时间起"
            title="创建时间起（含当天）"
            value={createdFrom}
            onChange={(e) => setCreatedFrom(e.target.value)}
          />
          <span className="text-sm text-muted">至</span>
          <input
            type="date"
            className="field w-auto"
            aria-label="创建时间止"
            title="创建时间止（含当天）"
            value={createdTo}
            onChange={(e) => setCreatedTo(e.target.value)}
          />
          {user.role === 'admin' && (
            <select
              className="field w-auto"
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
          {assets.length > 0 && (
            <button onClick={toggleSelectAll} className="btn-quiet">
              {allVisibleSelected ? '取消全选' : `全选 (${assets.length})`}
            </button>
          )}
          {selected.size > 0 && (
            <>
              <button onClick={() => setSelected(new Set())} className="btn-quiet">
                清除选择
              </button>
              <button onClick={handleBatchDownload} className="btn btn-primary">
                下载 ({selected.size})
              </button>
              <button onClick={handleBatchDelete} className="btn btn-danger">
                删除 ({selected.size})
              </button>
            </>
          )}
        </div>

        {/* 类型分栏：图片 / 视频 / 音频。放在标签栏之前 —— 先分大类，再按标签细分 */}
        <div className="mt-sm flex flex-wrap items-center gap-sm">
          {KIND_TABS.map((tab) => (
            <button
              key={tab.value || 'all'}
              type="button"
              onClick={() => setKindFilter(tab.value)}
              className="chip"
              data-state={kindFilter === tab.value ? 'on' : undefined}
              aria-pressed={kindFilter === tab.value}
            >
              {tab.label}
            </button>
          ))}
        </div>

        {/* 标签栏 */}
        {tags.length > 0 && (
          <div className="mt-sm flex flex-wrap gap-sm">
            <button
              onClick={() => setActiveTag(null)}
              className="chip"
              data-state={!activeTag ? 'on' : undefined}
            >
              全部
            </button>
            {tags.map((tag) => (
              <button
                key={tag.name}
                onClick={() => setActiveTag(activeTag === tag.name ? null : tag.name)}
                className="chip"
                data-state={activeTag === tag.name ? 'on' : undefined}
              >
                {tag.name} ({tag.count})
              </button>
            ))}
          </div>
        )}
      </div>

      {/* 素材画廊 */}
      {loading ? (
        <div
          className="mt-md grid grid-cols-2 gap-sm sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5"
          aria-hidden="true"
        >
          {Array.from({ length: 10 }).map((_, i) => (
            <div key={i}>
              <div className="skeleton aspect-square" />
              <div className="skeleton mt-2xs h-sm" />
            </div>
          ))}
        </div>
      ) : assets.length === 0 ? (
        <p className="mt-md flex min-h-[10rem] items-center justify-center border border-dashed border-rule text-base text-muted">
          暂无素材，去生成页面创建内容吧
        </p>
      ) : (
        <div className="mt-md grid grid-cols-2 gap-sm sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5">
          {assets.map((asset) => (
            <div key={asset.id} className="relative">
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
                <span className="absolute right-xs top-xs border border-rule bg-paper px-2xs text-xs text-ink-2">
                  {asset.is_shared && isMine(asset)
                    ? '共享中'
                    : asset.is_shared
                      ? `来自 ${asset.creator_name}`
                      : asset.creator_name}
                </span>
              )}

              {/* 视频角标：网格里视频首帧和图片长得一样，标一下免得点开才知道 */}
              {(asset.mime_type || '').startsWith('video/') && (
                <span className="pointer-events-none absolute bottom-6 left-xs border border-rule bg-paper px-2xs text-xs text-ink-2">
                  视频
                </span>
              )}

              <button
                type="button"
                onClick={() => setDetail(asset)}
                className="block w-full text-left"
              >
                <span className="block aspect-square overflow-hidden border border-rule bg-paper-2">
                  {(asset.mime_type || '').startsWith('audio/') ? (
                    // 音频没有画面，也不值得为了一个格子把整段音频拉下来
                    <span className="flex h-full w-full items-center justify-center px-xs text-center text-xs text-muted">
                      ♪ {asset.file_name}
                    </span>
                  ) : (
                    <AuthMedia
                      src={assetUrl(asset)}
                      posterSrc={getAssetThumbUrl(asset.task_id, asset.file_name)}
                      alt={asset.file_name}
                      mime={asset.mime_type}
                      className="h-full w-full object-cover"
                      loading="lazy"
                    />
                  )}
                </span>
                <span className="mt-2xs block">
                  <span className="flex flex-wrap gap-2xs">
                    {(asset.tags || []).slice(0, 3).map((tag) => (
                      <span
                        key={tag}
                        className="border border-rule bg-paper-2 px-2xs text-xs text-ink-2"
                      >
                        {tag}
                      </span>
                    ))}
                  </span>
                  <span className="mt-2xs block text-xs text-muted">
                    {new Date(asset.created_at).toLocaleDateString('zh-CN')}
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
            className="flex max-h-[90dvh] w-full max-w-3xl overflow-hidden border border-rule bg-paper"
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
            <div className="w-72 overflow-y-auto p-lg">
              <div className="flex items-center justify-between gap-xs">
                <h3 className="font-display text-title">素材详情</h3>
                <button
                  onClick={() => setDetail(null)}
                  className="x-btn"
                  aria-label="关闭素材详情"
                >
                  ✕
                </button>
              </div>

              <dl className="mt-md text-sm">
                <div className="rule-t py-xs">
                  <dt className="text-xs text-muted">文件</dt>
                  <dd className="text-ink">{detail.file_name}</dd>
                </div>
                <div className="rule-t py-xs">
                  <dt className="text-xs text-muted">大小</dt>
                  <dd className="num text-ink">{formatSize(detail.file_size)}</dd>
                </div>
                <div className="rule-t py-xs">
                  <dt className="text-xs text-muted">生成时间</dt>
                  <dd className="text-ink">
                    {new Date(detail.created_at).toLocaleString('zh-CN')}
                  </dd>
                </div>
                <div className="rule-t py-xs">
                  <dt className="text-xs text-muted">归属</dt>
                  <dd className="text-ink">
                    {isMine(detail) ? '我' : detail.creator_name || '无主数据'}
                    {detail.is_shared && (
                      <span className="ml-2xs border border-rule bg-paper-2 px-2xs text-xs text-ink-2">
                        已共享给团队
                      </span>
                    )}
                  </dd>
                </div>
              </dl>

              {/* 共享开关（仅本人/管理员）：共享的是「看」的权限，不是共同所有 */}
              {canModify(detail) && (
                <div className="mt-md border border-rule bg-paper-2 p-sm">
                  <div className="text-xs text-muted">
                    {detail.is_shared
                      ? '当前共享给团队：队友可见可下载，但不能改'
                      : '当前仅自己可见（admin 不受此限制）'}
                  </div>
                  <button
                    type="button"
                    onClick={() => handleToggleShare(detail)}
                    className="btn btn-quiet mt-xs"
                  >
                    {detail.is_shared ? '取消共享' : '共享给团队'}
                  </button>
                </div>
              )}

              {/* 标签管理：别人的共享素材只能看 —— 打标签是作者/管理员的权利 */}
              <div className="mt-md">
                <div className="text-xs text-muted">标签</div>
                <div className="mt-2xs flex flex-wrap gap-2xs">
                  {(detail.tags || []).map((tag) => (
                    <span
                      key={tag}
                      className="chip"
                    >
                      {tag}
                      {canModify(detail) && (
                        <button
                          onClick={() => handleRemoveTag(detail.id, tag)}
                          className="btn-quiet"
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
                    onKeyDown={(e) => {
                      if (e.key === 'Enter') {
                        handleAddTag(detail.id, (e.target as HTMLInputElement).value)
                        ;(e.target as HTMLInputElement).value = ''
                      }
                    }}
                  />
                )}
              </div>

              {/* 生成参数 */}
              {detail.meta && Object.keys(detail.meta).length > 0 && (
                <div className="mt-md">
                  <div className="text-xs text-muted">生成参数</div>
                  <dl className="mt-2xs border border-rule bg-paper-2 p-sm">
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
                      alert(`下载失败: ${err instanceof Error ? err.message : err}`),
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
