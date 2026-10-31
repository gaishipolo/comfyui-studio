import { useEffect, useState } from 'react'
import { fetchAssetBlob } from '../api/client'
import { rawFailureState, thumbnailFailureAction } from './authMediaState'

interface AuthMediaProps {
  /** 素材路径 —— 必须传 getAssetUrl() 的返回值（已含 /api 前缀），会带 JWT 去取 */
  src: string
  alt: string
  /** 素材 MIME，形如 image/png、video/mp4。缺省当图片处理 */
  mime?: string
  className?: string
  loading?: 'lazy' | 'eager'
  /** 视频/音频是否带播放控制条。缩略图给 false（静音只显示首帧），详情面板给 true */
  controls?: boolean
  /** 视频/图片缩略图地址。只有明确 404 时才退回原文件。 */
  posterSrc?: string
}

type MediaState =
  | { status: 'loading' }
  | { status: 'ready'; url: string; isPoster: boolean }
  | { status: 'missing' }
  | { status: 'failed' }

/**
 * 带鉴权的素材渲染（图片 / 视频 / 音频）。
 *
 * 素材接口需要 JWT，不能直接把服务端 URL 交给媒体标签。每次 src 变化都
 * 取消旧 fetch，并由本组件完整持有和回收对应 object URL。
 */
export default function AuthMedia({
  src,
  alt,
  mime,
  className,
  loading,
  controls = false,
  posterSrc,
}: AuthMediaProps) {
  const [state, setState] = useState<MediaState>({ status: 'loading' })

  useEffect(() => {
    const controller = new AbortController()
    let objectUrl: string | null = null
    setState({ status: 'loading' })

    const publish = (blob: Blob, isPoster: boolean) => {
      const nextUrl = URL.createObjectURL(blob)
      if (controller.signal.aborted) {
        URL.revokeObjectURL(nextUrl)
        return
      }
      objectUrl = nextUrl
      setState({ status: 'ready', url: nextUrl, isPoster })
    }

    const loadRaw = async () => {
      try {
        const blob = await fetchAssetBlob(src, { signal: controller.signal })
        publish(blob, false)
      } catch (error) {
        if (controller.signal.aborted) return
        const failure = rawFailureState(error)
        if (failure === 'missing') setState({ status: 'missing' })
        else if (failure === 'failed') setState({ status: 'failed' })
      }
    }

    const load = async () => {
      if (posterSrc) {
        try {
          const blob = await fetchAssetBlob(posterSrc, { signal: controller.signal })
          publish(blob, true)
          return
        } catch (error) {
          if (controller.signal.aborted) return
          const action = thumbnailFailureAction(error)
          if (action === 'aborted') return
          if (action === 'failed') {
            setState({ status: 'failed' })
            return
          }
          // Only a thumbnail 404 reaches the original-file request below.
        }
      }
      await loadRaw()
    }

    void load()
    return () => {
      controller.abort()
      if (objectUrl) URL.revokeObjectURL(objectUrl)
    }
  }, [src, posterSrc])

  const handleDecodeError = () => {
    if (state.status !== 'ready') return
    URL.revokeObjectURL(state.url)
    setState({ status: 'failed' })
  }

  if (state.status === 'loading') return null

  if (state.status === 'missing' || state.status === 'failed') {
    const missing = state.status === 'missing'
    const label = missing ? '文件已丢失' : '素材加载失败'
    return (
      <div
        className={`flex items-center justify-center bg-paper-2 text-xs text-muted ${className ?? ''}`}
        role="img"
        aria-label={`${alt}（${label}）`}
      >
        {label}
      </div>
    )
  }

  if (state.isPoster) {
    return (
      <img
        src={state.url}
        alt={alt}
        className={className}
        loading={loading}
        onError={handleDecodeError}
      />
    )
  }

  const kind = (mime ?? '').split('/')[0]

  if (kind === 'video') {
    return (
      <video
        src={state.url}
        className={className}
        controls={controls}
        muted={!controls}
        playsInline
        preload="metadata"
        aria-label={alt}
        onError={handleDecodeError}
      />
    )
  }

  if (kind === 'audio') {
    return controls ? (
      <audio
        src={state.url}
        controls
        className={className}
        aria-label={alt}
        onError={handleDecodeError}
      />
    ) : (
      <div
        className={`flex items-center justify-center bg-paper-2 text-xs text-muted ${className ?? ''}`}
        role="img"
        aria-label={alt}
      >
        ♪ {alt}
      </div>
    )
  }

  return (
    <img
      src={state.url}
      alt={alt}
      className={className}
      loading={loading}
      onError={handleDecodeError}
    />
  )
}
