/** 自绘弹窗的键盘可达性 —— Escape 关闭 + Tab 焦点圈定 + 打开时聚焦首个控件。
 *
 *  此前三个自绘弹窗只能点遮罩关闭：键盘用户按 Escape 没反应，
 *  Tab 还会跑到弹窗背后的页面去（焦点丢失 = 弹窗对键盘用户不可用）。 */
import { useEffect, type RefObject } from 'react'
import { currentFocusIndex, resolveModalKey } from './modalA11y'

const FOCUSABLE_SELECTOR = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled]):not([type="hidden"])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(', ')

function focusableWithin(container: HTMLElement | null): HTMLElement[] {
  if (!container) return []
  return Array.from(container.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR)).filter(
    (element) => element.offsetParent !== null || element === document.activeElement,
  )
}

/**
 * 挂在弹窗容器上：Escape 触发 onClose，Tab 在容器内循环。
 * 容器需要 role="dialog" aria-modal="true" 与 tabIndex={-1}。
 */
export function useModalKeyboard(
  active: boolean,
  containerRef: RefObject<HTMLElement | null>,
  onClose: () => void,
): void {
  useEffect(() => {
    if (!active) return
    const container = containerRef.current
    if (!container) return

    const initial = focusableWithin(container)[0]
    initial?.focus()

    const handleKeyDown = (event: KeyboardEvent) => {
      const focusable = focusableWithin(container)
      const action = resolveModalKey(
        event.key,
        event.shiftKey,
        focusable.length,
        currentFocusIndex(document.activeElement, focusable),
      )
      if (action.type === 'escape') {
        event.preventDefault()
        onClose()
        return
      }
      if (action.type === 'focus') {
        event.preventDefault()
        if (action.index < 0) {
          container.focus()
          return
        }
        focusable[action.index]?.focus()
      }
    }

    document.addEventListener('keydown', handleKeyDown)
    return () => document.removeEventListener('keydown', handleKeyDown)
  }, [active, containerRef, onClose])
}
