/** 编辑器左栏 —— 节点 / 素材
 *
 * 节点栏是「往流程里加东西」的入口：点一下就把这个节点接到流程末尾，
 * 比「先加一个空步骤、再双击选节点」少两步。
 * 素材栏是参照物：编排时想看看手上有哪些图（比如要给图生图挑一张参考）。
 * 它只读 —— 往流程里固定塞一张素材是另一件事（流程输入得能在运行时换）。
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import type { Asset, NodeCapability } from '../../types'
import { getAssetThumbUrl, getAssetUrl, listAssets } from '../../api/client'
import AuthMedia from '../AuthMedia'
import { nodeVisual } from '../flowEditor'

type RailTab = 'nodes' | 'assets'

const CAPABILITY_LABEL: Record<string, string> = {
  text_to_image: '文生图',
  image_to_image: '图生图',
  text_to_video: '文生视频',
  image_to_video: '图生视频',
  upscale: '放大',
}

export default function NodeRail({
  caps,
  onAddNode,
}: {
  caps: NodeCapability[]
  /** 点节点 → 以这个节点新建一步（接在末尾） */
  onAddNode: (cap: NodeCapability) => void
}) {
  const [tab, setTab] = useState<RailTab>('nodes')
  const [keyword, setKeyword] = useState('')
  const [assets, setAssets] = useState<Asset[]>([])
  const [assetsLoading, setAssetsLoading] = useState(false)
  const [assetsFailed, setAssetsFailed] = useState(false)

  // 素材只在第一次切到这一栏时拉 —— 不切就不该为它发请求
  //
  // 用 ref 记「拉过没」，而不是把 loading 放进依赖：setAssetsLoading 会触发
  // 重渲染 → effect 重跑 → cleanup 把 alive 置 false → 回来的数据被丢掉。
  // 那个坑很隐蔽（请求发出去了，就是永远不显示）。
  const assetsLoaded = useRef(false)
  useEffect(() => {
    if (tab !== 'assets' || assetsLoaded.current) return
    assetsLoaded.current = true
    let alive = true
    setAssetsLoading(true)
    setAssetsFailed(false)
    listAssets({ limit: '24' })
      .then((data) => {
        if (alive) setAssets(Array.isArray(data) ? data : [])
      })
      .catch(() => {
        if (alive) setAssetsFailed(true)
      })
      .finally(() => {
        if (alive) setAssetsLoading(false)
      })
    return () => {
      alive = false
    }
  }, [tab])

  const shown = useMemo(() => {
    const kw = keyword.trim().toLowerCase()
    if (!kw) return caps
    return caps.filter(
      (c) =>
        c.name.toLowerCase().includes(kw) ||
        (CAPABILITY_LABEL[c.capability] ?? c.capability).includes(kw) ||
        c.engine.toLowerCase().includes(kw),
    )
  }, [caps, keyword])

  return (
    <aside
      className="flex w-56 shrink-0 flex-col gap-xs rounded-lg border border-tertiary bg-paper p-sm"
      data-app-rail="left"
    >
      <div className="flex rounded-lg border border-tertiary p-2xs" role="tablist">
        {(
          [
            ['nodes', '节点'],
            ['assets', '素材'],
          ] as [RailTab, string][]
        ).map(([key, label]) => (
          <button
            key={key}
            type="button"
            role="tab"
            aria-selected={tab === key}
            className={`flex-1 rounded px-sm py-2xs text-sm ${
              tab === key ? 'bg-secondary font-medium' : 'text-muted'
            }`}
            onClick={() => setTab(key)}
          >
            {label}
          </button>
        ))}
      </div>

      {tab === 'nodes' ? (
        <>
          <input
            className="field"
            placeholder="搜节点"
            value={keyword}
            onChange={(e) => setKeyword(e.target.value)}
            aria-label="搜索节点"
          />
          <p className="text-xs text-muted">点一下接到流程末尾</p>
          <ul className="flex min-h-0 min-w-0 flex-1 flex-col gap-2xs overflow-y-auto overflow-x-hidden">
            {shown.length === 0 ? (
              <li className="text-xs text-muted">
                {caps.length === 0 ? '还没有可用节点（去「管理」加一个模板）' : '没有匹配的节点'}
              </li>
            ) : (
              shown.map((cap) => {
                const visual = nodeVisual(cap)
                return (
                  <li key={cap.id}>
                    <button
                      type="button"
                      data-add-node={cap.id}
                      className="w-full rounded-lg border border-tertiary p-sm text-left hover:border-rule-strong hover:bg-secondary"
                      onClick={() => onAddNode(cap)}
                      title={`加到流程末尾：${cap.name}`}
                    >
                      <span className="flex items-center gap-xs">
                        <span aria-hidden="true">{visual.icon}</span>
                        <span className="min-w-0 flex-1 truncate text-sm">{cap.name}</span>
                      </span>
                      <span className="mt-2xs block text-xs text-muted">
                        {CAPABILITY_LABEL[cap.capability] ?? cap.capability} · {cap.engine}
                      </span>
                    </button>
                  </li>
                )
              })
            )}
          </ul>
        </>
      ) : (
        <div className="min-h-0 min-w-0 flex-1 overflow-y-auto overflow-x-hidden">
          {assetsLoading && assets.length === 0 ? (
            <p className="text-xs text-muted">加载中…</p>
          ) : assetsFailed ? (
            <p className="text-xs text-error">素材加载失败</p>
          ) : assets.length === 0 ? (
            <p className="text-xs text-muted">素材库还是空的</p>
          ) : (
            <>
              <p className="mb-2xs text-xs text-muted">最近的素材（只读参照）</p>
              <ul className="grid grid-cols-3 gap-2xs">
                {assets.map((asset) => (
                  <li key={asset.id}>
                    <a
                      href={getAssetUrl(asset.id)}
                      target="_blank"
                      rel="noreferrer"
                      className="block aspect-square overflow-hidden rounded border border-tertiary hover:border-accent"
                      title={asset.file_name}
                    >
                      {(asset.mime_type || '').startsWith('audio/') ? (
                        <span className="flex h-full w-full items-center justify-center text-xs text-muted">
                          ♪
                        </span>
                      ) : (
                        <AuthMedia
                          src={getAssetUrl(asset.id)}
                          posterSrc={getAssetThumbUrl(asset.id)}
                          alt={asset.file_name}
                          mime={asset.mime_type}
                          className="h-full w-full object-cover"
                          loading="lazy"
                        />
                      )}
                    </a>
                  </li>
                ))}
              </ul>
            </>
          )}
        </div>
      )}
    </aside>
  )
}
