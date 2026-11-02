/** 弹窗键盘语义 —— 纯函数部分，可离线断言。
 *
 *  自绘弹窗（素材详情 / 素材选择 / 改密）此前只支持点遮罩关闭：
 *  键盘用户按 Escape 没反应、Tab 会跑到弹窗背后的页面去。
 *  这里定义「一次按键 → 做什么」的判定，React 侧只负责执行。 */

export type ModalKeyAction =
  | { type: 'escape' }
  | { type: 'focus'; index: number }
  | { type: 'none' }

/** 当前聚焦项在可聚焦列表中的下标；不在列表里返回 -1 */
export function currentFocusIndex(
  active: Element | null,
  focusable: readonly HTMLElement[],
): number {
  if (!active) return -1
  return focusable.indexOf(active as HTMLElement)
}

/** 按一次键应该做什么 */
export function resolveModalKey(
  key: string,
  shiftKey: boolean,
  focusableCount: number,
  currentIndex: number,
): ModalKeyAction {
  if (key === 'Escape') return { type: 'escape' }
  if (key !== 'Tab') return { type: 'none' }
  if (focusableCount <= 0) return { type: 'focus', index: -1 }
  // 焦点还没进弹窗（currentIndex < 0）：Tab 落到第一个控件，Shift+Tab 落到最后一个
  if (currentIndex < 0) {
    return { type: 'focus', index: shiftKey ? focusableCount - 1 : 0 }
  }
  const offset = shiftKey ? -1 : 1
  return { type: 'focus', index: (currentIndex + offset + focusableCount) % focusableCount }
}
