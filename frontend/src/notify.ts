/** 应用内通知（P1-4）—— 任务完成/失败的 toast
 *
 * 任务完成检测在 App 层全局轮询（不随页面切换中断），这里只管「弹一条」。
 * notifyOnce 按 key 去重，即使有多个观察者也只报一次。
 * 另外提供素材变更事件：任务完成后素材库要自动刷新，靠这个信号驱动。 */

export interface Toast {
  id: number
  text: string
  tone: 'success' | 'error'
}

let toasts: Toast[] = []
let seq = 0
const listeners = new Set<(t: Toast[]) => void>()
/** 已通知过的 key（task_id:终态），防重复报 */
const notified = new Set<string>()
/** 素材变更监听器 —— 任务跑完后素材库订阅此事件自动刷新 */
const assetListeners = new Set<() => void>()

function emit() {
  for (const fn of listeners) fn([...toasts])
}

/** 通知「素材库该刷新了」（任务完成/失败产生或清除了素材时触发） */
export function emitAssetsChanged() {
  for (const fn of assetListeners) fn()
}

/** 订阅素材变更，返回退订函数 */
export function subscribeAssetsChanged(fn: () => void): () => void {
  assetListeners.add(fn)
  return () => {
    assetListeners.delete(fn)
  }
}

/** 弹一条 toast，6 秒后自动消失（也可点击关掉） */
export function notify(text: string, tone: Toast['tone'] = 'success') {
  const id = ++seq
  toasts = [...toasts, { id, text, tone }]
  emit()
  setTimeout(() => dismiss(id), 6000)
}

/** 按 key 只弹一次 —— 同一任务的同一终态不会被两个轮询器重复报 */
export function notifyOnce(key: string, text: string, tone: Toast['tone'] = 'success') {
  if (notified.has(key)) return
  notified.add(key)
  notify(text, tone)
}

export function dismiss(id: number) {
  toasts = toasts.filter((t) => t.id !== id)
  emit()
}

export function subscribe(fn: (t: Toast[]) => void): () => void {
  listeners.add(fn)
  fn([...toasts])
  return () => {
    listeners.delete(fn)
  }
}

/** 测试/登出时清干净 */
export function resetNotifications() {
  toasts = []
  notified.clear()
  emit()
}
