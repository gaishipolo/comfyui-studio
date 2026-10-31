export interface CompletedUpload<TPreview> {
  value: string
  preview: TPreview
}

interface UploadCompletionOptions<TPreview> {
  isCurrent?: () => boolean
  disposePreview?: (preview: TPreview) => void
}

/** Keep preview construction behind the engine-forwarding success boundary. */
export async function completeUpload<TPreview>(
  forward: () => Promise<string>,
  createPreview: () => TPreview,
  fallbackPreview: TPreview,
  options?: UploadCompletionOptions<TPreview>,
): Promise<CompletedUpload<TPreview> | null> {
  const value = await forward()
  const isCurrent = options?.isCurrent ?? (() => true)
  if (!isCurrent()) return null

  let preview: TPreview
  try {
    preview = createPreview()
  } catch {
    preview = fallbackPreview
  }
  if (!isCurrent()) {
    options?.disposePreview?.(preview)
    return null
  }
  return { value, preview }
}
