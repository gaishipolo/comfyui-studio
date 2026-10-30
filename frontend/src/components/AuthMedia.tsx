import { useEffect, useState } from 'react'
import { fetchAssetBlob } from '../api/client'

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
}

/**
 * 带鉴权的素材渲染（图片 / 视频 / 音频）。
 *
 * 不能直接 <img src={getAssetUrl(...)} /> —— 素材接口要 JWT，
 * 而 <img>/<video> 标签发不出 Authorization 头，结果是整排缩略图 401。
 * 这里用 fetch 带头取二进制，再以 object URL 渲染。
 *
 * 之所以要按 MIME 分流：之前一律用 <img>，视频任务跑完把 mp4 送回来也是一片空白
 * —— <img> 根本不认 video/mp4。
 */
export default function AuthMedia({
  src,
  alt,
  mime,
  className,
  loading,
  controls = false,
}: AuthMediaProps) {
  const [url, setUrl] = useState<string | null>(null)
  const [failed, setFailed] = useState(false)

  useEffect(() => {
    let objectUrl: string | null = null
    let cancelled = false

    setUrl(null)
    setFailed(false)

    fetchAssetBlob(src)
      .then((blob) => {
        if (cancelled) return
        objectUrl = URL.createObjectURL(blob)
        setUrl(objectUrl)
      })
      .catch(() => {
        if (!cancelled) setFailed(true)
      })

    return () => {
      cancelled = true
      if (objectUrl) URL.revokeObjectURL(objectUrl)
    }
  }, [src])

  // 加载中：保持容器占位（外层 aspect-square 撑住布局），不闪一下再变
  if (!url) {
    return failed ? (
      // 取不到就是文件已经不在了（被删过、或磁盘清理过）——
      // 如实说一声，别把「没了」表现成「还在加载」
      <div
        className={`flex items-center justify-center bg-paper-2 text-xs text-muted ${className ?? ''}`}
        role="img"
        aria-label={`${alt}（文件已丢失）`}
      >
        文件已丢失
      </div>
    ) : null
  }

  const kind = (mime ?? '').split('/')[0]

  if (kind === 'video') {
    return (
      <video
        src={url}
        className={className}
        controls={controls}
        // 缩略图不该一播放就出声
        muted={!controls}
        playsInline
        preload="metadata"
        aria-label={alt}
      />
    )
  }

  if (kind === 'audio') {
    return controls ? (
      <audio src={url} controls className={className} aria-label={alt} />
    ) : (
      // 音频没有画面，缩略图位置给个明确占位，免得一片空白看着像没加载出来
      <div
        className={`flex items-center justify-center bg-paper-2 text-xs text-muted ${className ?? ''}`}
        role="img"
        aria-label={alt}
      >
        ♪ {alt}
      </div>
    )
  }

  return <img src={url} alt={alt} className={className} loading={loading} />
}
