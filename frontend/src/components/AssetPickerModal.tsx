/** 素材选择弹窗 — 从素材库挑已有素材作为工作流的参考输入
 *
 *  缩略图必须走 AuthMedia：素材接口带 JWT，<img src> 直连发不出
 *  Authorization 头，会整排 401。
 *
 *  按 kind 过滤（image / video / audio）由调用方决定 —— 音频字段只能选音频，
 *  这里不做「切换类型」的入口：能选什么是字段声明的，不是用户临时决定的。
 *  multiple 时进入多选模式，点格子只切换勾选，点「确定」才提交。 */
import { useEffect, useState } from 'react'
import type { Asset } from '../types'
import { listAssets, getAssetUrl, getAssetThumbUrl } from '../api/client'
import AuthMedia from './AuthMedia'

export type AssetKind = 'image' | 'video' | 'audio'

interface Props {
  /** 只看这类素材，由字段的 accept 决定 */
  kind?: AssetKind
  /** true = 可多选（对应列表字段） */
  multiple?: boolean
  onSelect: (assets: Asset[]) => void
  onClose: () => void
}

/** 一次取最近 60 个，搜索可以翻到更早的（弹窗是挑素材，不做完整翻页） */
const PAGE_SIZE = 60

const KIND_LABEL: Record<AssetKind, string> = {
  image: '图片',
  video: '视频',
  audio: '音频',
}

export default function AssetPickerModal({
  kind = 'image',
  multiple = false,
  onSelect,
  onClose,
}: Props) {
  const [keyword, setKeyword] = useState('')
  const [debounced, setDebounced] = useState('')
  const [assets, setAssets] = useState<Asset[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  // 错误态的「重试」用它把 fetch 效果重跑一遍
  const [reloadKey, setReloadKey] = useState(0)
  // 多选模式的已选集合（单选模式不用它）
  const [picked, setPicked] = useState<Set<string>>(new Set())

  // 搜索防抖 300ms，与素材库一致
  useEffect(() => {
    const timer = setTimeout(() => setDebounced(keyword.trim()), 300)
    return () => clearTimeout(timer)
  }, [keyword])

  // 换关键词 / 换类型都清空已选 —— 免得确认时提交了看不见的素材
  useEffect(() => {
    setPicked(new Set())
  }, [debounced, kind])

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError('')
    const params: Record<string, string> = { limit: String(PAGE_SIZE), kind }
    if (debounced) params.keyword = debounced
    listAssets(params)
      .then((data) => {
        if (cancelled) return
        // 后端已按 kind 过滤；这里再兜一次，防老后端不认这个参数
        setAssets(
          Array.isArray(data) ? data.filter((a) => (a.mime_type || '').startsWith(`${kind}/`)) : [],
        )
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

  const togglePick = (id: string) =>
    setPicked((prev) => {
      const next = new Set(prev)
      next.has(id) ? next.delete(id) : next.add(id)
      return next
    })

  const confirm = () => {
    const chosen = assets.filter((a) => picked.has(a.id))
    if (chosen.length) onSelect(chosen)
  }

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
          <div className="flex items-baseline gap-xs">
            <h3 className="font-display text-title">从素材库选择</h3>
            <span className="border border-rule bg-paper-2 px-2xs text-xs text-ink-2">
              只看{KIND_LABEL[kind]}
            </span>
          </div>
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
              {debounced
                ? `没有匹配「${debounced}」的${KIND_LABEL[kind]}`
                : `素材库还没有可用的${KIND_LABEL[kind]}`}
            </p>
          ) : (
            <div className="grid grid-cols-2 gap-sm sm:grid-cols-3 md:grid-cols-4">
              {assets.map((asset) => {
                const on = picked.has(asset.id)
                return (
                  <button
                    key={asset.id}
                    type="button"
                    onClick={() => (multiple ? togglePick(asset.id) : onSelect([asset]))}
                    title={asset.file_name}
                    aria-pressed={multiple ? on : undefined}
                    className="block w-full text-left"
                  >
                    <span
                      className={`relative block aspect-square overflow-hidden border bg-paper-2 ${
                        on ? 'border-accent ring-2 ring-accent' : 'border-rule'
                      }`}
                    >
                      {kind === 'audio' ? (
                        // 音频没有画面，也不值得为了一张格子把整段音频拉下来
                        <span className="flex h-full w-full items-center justify-center px-xs text-center text-xs text-muted">
                          ♪ {asset.file_name}
                        </span>
                      ) : (
                        <AuthMedia
                          src={getAssetUrl(asset.task_id, asset.file_name)}
                          posterSrc={getAssetThumbUrl(asset.task_id, asset.file_name)}
                          alt={asset.file_name}
                          mime={asset.mime_type}
                          className="h-full w-full object-cover"
                          loading="lazy"
                        />
                      )}
                      {kind === 'video' && (
                        <span className="absolute bottom-xs left-xs border border-rule bg-paper px-2xs text-xs text-ink-2">
                          视频
                        </span>
                      )}
                      {multiple && (
                        <span
                          className={`absolute left-xs top-xs flex h-5 w-5 items-center justify-center border text-xs ${
                            on ? 'border-accent bg-accent text-paper' : 'border-rule bg-paper text-transparent'
                          }`}
                          aria-hidden="true"
                        >
                          ✓
                        </span>
                      )}
                    </span>
                    <span className="mt-2xs block truncate text-xs text-muted">
                      {asset.file_name}
                    </span>
                  </button>
                )
              })}
            </div>
          )}
        </div>

        {/* 多选模式才有的底部操作条 */}
        {multiple && (
          <div className="flex items-center justify-between gap-sm border-t border-rule p-md">
            <span className="text-sm text-muted">
              {picked.size > 0 ? `已选 ${picked.size} 个` : '点素材切换选中'}
            </span>
            <div className="flex gap-xs">
              <button type="button" className="btn-quiet" onClick={onClose}>
                取消
              </button>
              <button
                type="button"
                className="btn btn-primary"
                disabled={picked.size === 0}
                onClick={confirm}
              >
                确定{picked.size > 0 ? ` (${picked.size})` : ''}
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
