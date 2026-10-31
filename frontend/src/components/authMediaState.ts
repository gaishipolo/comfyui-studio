type StatusError = { status: number }

const hasStatus = (error: unknown): error is StatusError =>
  typeof error === 'object' &&
  error !== null &&
  typeof (error as { status?: unknown }).status === 'number'

export const isAbortError = (error: unknown): boolean =>
  typeof error === 'object' &&
  error !== null &&
  (error as { name?: unknown }).name === 'AbortError'

export type ThumbnailFailureAction = 'aborted' | 'fallback' | 'failed'

export function thumbnailFailureAction(error: unknown): ThumbnailFailureAction {
  if (isAbortError(error)) return 'aborted'
  return hasStatus(error) && error.status === 404 ? 'fallback' : 'failed'
}

export type RawFailureState = 'aborted' | 'missing' | 'failed'

export function rawFailureState(error: unknown): RawFailureState {
  if (isAbortError(error)) return 'aborted'
  return hasStatus(error) && error.status === 404 ? 'missing' : 'failed'
}
