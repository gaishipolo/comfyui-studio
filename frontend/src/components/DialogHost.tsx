/** 应用内对话框的宿主 —— 挂在 App 根部，全局只有一个实例。
 *
 *  焦点与键盘行为复用 `useModalKeyboard`：Esc = 取消，Tab 在弹窗内循环，
 *  打开时聚焦首个控件（DOM 顺序上「取消」在前，所以危险操作默认不落在确认上）。
 */
import { useCallback, useRef } from 'react'
import type { DialogTone } from './dialog'
import { settleDialog, useCurrentDialog } from './dialog'
import { useModalKeyboard } from './useModalKeyboard'

/** 图标外圈的底色与前景色 —— 只换色，形状统一用一个圆 */
const TONE_STYLE: Record<DialogTone, string> = {
  default: 'bg-accent-soft text-accent',
  danger: 'bg-error-soft text-error',
  error: 'bg-error-soft text-error',
  warning: 'bg-warning-soft text-warning',
  success: 'bg-success-soft text-success',
}

const ICON_PATH: Record<DialogTone, string> = {
  default: 'M12 8h.01M11 12h1v4h1',
  danger: 'M12 9v4m0 3h.01',
  error: 'M12 9v4m0 3h.01',
  warning: 'M12 9v4m0 3h.01',
  success: 'M8 12.5l2.5 2.5L16 9.5',
}

export default function DialogHost() {
  const pending = useCurrentDialog()
  const dialogRef = useRef<HTMLDivElement>(null)
  const cancel = useCallback(() => settleDialog(false), [])
  const active = Boolean(pending)

  useModalKeyboard(active, dialogRef, cancel)

  if (!pending) return null

  const { req } = pending
  const tone: DialogTone = req.tone ?? 'default'
  const isDanger = tone === 'danger' || tone === 'error'
  const showCancel = req.cancelText !== null

  return (
    <div
      className="fixed inset-0 z-modal flex items-center justify-center bg-scrim p-md"
      onClick={cancel}
    >
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="app-dialog-title"
        aria-describedby={req.message ? 'app-dialog-message' : undefined}
        tabIndex={-1}
        className="w-full max-w-md overflow-hidden rounded-lg border border-rule bg-paper shadow-lg"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-start gap-sm p-lg">
          <span
            aria-hidden="true"
            className={`grid h-9 w-9 shrink-0 place-items-center rounded-pill ${TONE_STYLE[tone]}`}
          >
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" aria-hidden="true">
              <circle cx="12" cy="12" r="9" stroke="currentColor" strokeWidth="1.6" />
              <path
                d={ICON_PATH[tone]}
                stroke="currentColor"
                strokeWidth="1.8"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
          </span>
          <div className="min-w-0 flex-1 pt-2xs">
            <h3 id="app-dialog-title" className="font-display text-title font-semibold text-ink">
              {req.title}
            </h3>
            {req.message && (
              <p
                id="app-dialog-message"
                className="mt-xs whitespace-pre-line break-words text-sm leading-relaxed text-ink-2"
              >
                {req.message}
              </p>
            )}
          </div>
        </div>

        <div className="flex justify-end gap-xs border-t border-rule bg-paper-2 px-lg py-sm">
          {showCancel && (
            <button type="button" className="btn" onClick={cancel}>
              {req.cancelText}
            </button>
          )}
          <button
            type="button"
            className={isDanger && showCancel ? 'btn-danger-solid' : 'btn-primary'}
            onClick={() => settleDialog(true)}
          >
            {req.confirmText ?? '确定'}
          </button>
        </div>
      </div>
    </div>
  )
}
