export interface CompletedUpload<TPreview> {
  value: string
  preview: TPreview
}

/** Keep preview construction behind the engine-forwarding success boundary. */
export async function completeUpload<TPreview>(
  forward: () => Promise<string>,
  createPreview: () => TPreview,
  fallbackPreview: TPreview,
): Promise<CompletedUpload<TPreview>> {
  const value = await forward()
  try {
    return { value, preview: createPreview() }
  } catch {
    return { value, preview: fallbackPreview }
  }
}
