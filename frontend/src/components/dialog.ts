/** 应用内对话框 —— 替代原生 `alert()` / `confirm()`
 *
 *  为什么不用原生：`confirm` 是**同步阻塞**的，弹窗期间页面停在半截状态；
 *  而且样式完全由浏览器决定，和界面脱节（macOS 上是灰白方块，Windows 上是标题栏窗口）。
 *
 *  用法：
 *    if (!(await confirmDialog({ title: '删除素材？', tone: 'danger' }))) return
 *    await alertDialog({ title: '部分失败', message: '多行\n文本' })
 *
 *  同一时刻只显示一个，后来的排队 —— 并发请求不会叠成一摞。
 */
import { useEffect, useState } from 'react'

export type DialogTone = 'default' | 'danger' | 'success' | 'error' | 'warning'

export interface DialogRequest {
  title: string
  /** 正文；换行符会原样保留（`whitespace-pre-line`） */
  message?: string
  tone?: DialogTone
  confirmText?: string
  /** `null` = 信息型弹窗，只留一个「知道了」 */
  cancelText?: string | null
}

interface PendingDialog {
  id: number
  req: DialogRequest
  resolve: (ok: boolean) => void
}

let current: PendingDialog | null = null
const queue: PendingDialog[] = []
let seq = 0
const listeners = new Set<() => void>()

/** 让队首上屏（没有排队就只通知清空），并广播一次 */
function pump() {
  if (!current && queue.length > 0) current = queue.shift()!
  for (const fn of listeners) fn()
}

/** 弹一个确认框：确定 = true，取消 / Esc / 点遮罩 = false */
export function confirmDialog(req: DialogRequest): Promise<boolean> {
  return new Promise((resolve) => {
    queue.push({
      id: ++seq,
      req: { cancelText: '取消', ...req },
      resolve,
    })
    pump()
  })
}

/** 弹一个纯信息框（只有「知道了」），等用户读完再继续 */
export function alertDialog(req: DialogRequest): Promise<void> {
  return new Promise((resolve) => {
    queue.push({
      id: ++seq,
      req: { confirmText: '知道了', ...req, cancelText: null },
      resolve: () => resolve(),
    })
    pump()
  })
}

/** DialogHost 调用：关闭当前弹窗并放行排队中的下一个 */
export function settleDialog(ok: boolean) {
  const done = current
  if (!done) return
  current = null
  done.resolve(ok)
  pump()
}

export function subscribeDialog(fn: () => void): () => void {
  listeners.add(fn)
  return () => {
    listeners.delete(fn)
  }
}

/** 当前该显示的弹窗（引用每次变化都会触发重渲染） */
export function useCurrentDialog(): PendingDialog | null {
  const [state, setState] = useState<PendingDialog | null>(current)
  useEffect(() => {
    const sync = () => setState(current)
    const off = subscribeDialog(sync)
    sync()
    return off
  }, [])
  return state
}
