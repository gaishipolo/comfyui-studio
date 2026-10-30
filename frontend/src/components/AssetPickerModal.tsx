/** 素材选择弹窗 — 从素材库挑一张已有图片作为工作流的参考输入
 *
 *  缩略图必须走 AuthMedia：素材接口带 JWT，<img src> 直连发不出
 *  Authorization 头，会整排 401。
 *
 *  现在只放图片（kind='image'）；以后要支持视频，换 kind 即可 ——
 *  后端 /api/upload/from-asset 的 mime 前缀白名单同步扩一行。 */
import { useEffect, useState } from 'react'
import type { Asset } from '../types'
import { listAssets, getAssetUrl } from '../api/client'
import AuthMedia from './AuthMedia'

interface Props {
  /** mime 前缀过滤：'image' 只看图片，以后支持视频只换这个值 */
  kind?: 'image'
  onSelect: (asset: Asset) => void
  onClose: () => void
}

/** 一次取最近 60 张，搜索可以翻到更早的（弹窗是挑图，不做完整翻页） */
const PAGE_SIZE = 60

export default function AssetPickerModal({ kind = 'image', onSelect, onClose }: Props) {
  const [keyword, setKeyword] = useState('')
  const [debounced, setDebounced] = useState('')
  const [assets, setAssets] = useState<Asset[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  // 错误态的「重试」用它把 fetch 效果重跑一遍
  const [reloadKey, setReloadKey] = useState(0)

  // 搜索防抖 300ms，与素材库一致
  useEffect(() => {
    const timer = setTimeout(() => setDebounced(keyword.trim()), 300)
    return () => clearTimeout(timer)
  }, [keyword])

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError('')
    const params: Record<string, string> = { limit: String(PAGE_SIZE) }
    if (debounced) params.keyword = debounced
    listAssets(params)
      .then((data) => {
        if (cancelled) return
        // 后端列表接口暂不支持按 mime 筛选，取回来后本地按 kind 过滤
        setAssets(data.filter((a) => (a.mime_type || '').startsWith(`${kind}/`)))
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err))
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [debounced, kind, reloadKey])

  return (
    <div
      className="fixed inset-0 z-modal flex items-center justify-center bg-scrim p-md"
      onClick={onClose}
    >
      <div
        className="flex max-h-[85dvh] w-full max-w-3xl flex-col overflow-hidden border border-rule bg-paper"
        onClick={(e) => e.stopPropagation()}
      >
        {/* 头部 */}
        <div className="flex items-center justify-between gap-xs border-b border-rule p-md">
          <h3 className="font-display text-title">从素材库选择</h3>
          <button type="button" onClick={onClose} className="x-btn" aria-label="关闭素材选择">
            ✕
          </button>
        </div>

        {/* 搜索 */}
        <div className="border-b border-rule p-md">
          <input
            type="search"
            className="field"
            placeholder="搜索素材（文件名 / 提示词）"
            value={keyword}
            onChange={(e) => setKeyword(e.target.value)}
            // 弹窗可能挂在表单里，回车别误触提交（IME 组词中的回车不拦）
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.nativeEvent.isComposing) e.preventDefault()
            }}
            autoFocus
          />
        </div>

        {/* 四态：加载骨架 / 错误+重试 / 空 / 网格 */}
        <div className="flex-1 overflow-y-auto p-md">
          {loading ? (
            <div
              className="grid grid-cols-2 gap-sm sm:grid-cols-3 md:grid-cols-4"
              aria-hidden="true"
            >
              {Array.from({ length: 8 }).map((_, i) => (
                <div key={i}>
                  <div className="skeleton aspect-square" />
                  <div className="skeleton mt-2xs h-sm" />
                </div>
              ))}
            </div>
          ) : error ? (
            <div className="py-lg text-center">
              <p className="text-sm text-error">{error}</p>
              <button
                type="button"
                className="btn-quiet mt-sm"
                onClick={() => setReloadKey((k) => k + 1)}
              >
                重试
              </button>
            </div>
          ) : assets.length === 0 ? (
            <p className="flex min-h-[10rem] items-center justify-center border border-dashed border-rule text-base text-muted">
              {debounced ? `没有匹配「${debounced}」的素材` : '素材库还没有可用的图片'}
            </p>
          ) : (
            <div className="grid grid-cols-2 gap-sm sm:grid-cols-3 md:grid-cols-4">
              {assets.map((asset) => (
                <button
                  key={asset.id}
                  type="button"
                  onClick={() => onSelect(asset)}
                  title={asset.file_name}
                  className="block w-full text-left"
                >
                  <span className="block aspect-square overflow-hidden border border-rule bg-paper-2">
                    <AuthMedia
                      src={getAssetUrl(asset.task_id, asset.file_name)}
                      alt={asset.file_name}
                      mime={asset.mime_type}
                      className="h-full w-full object-cover"
                      loading="lazy"
                    />
                  </span>
                  <span className="mt-2xs block truncate text-xs text-muted">
                    {asset.file_name}
                  </span>
                </button>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
