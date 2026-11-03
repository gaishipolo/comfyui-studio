/** 服务端时间解析 —— 全站时间格式化统一入口。
 *
 *  背景：后端统一存 **naive UTC**（`timeutils.utcnow()`），Pydantic 序列化出来
 *  是 `"2026-11-02T16:00:00"` —— 没有 `Z` 也没有 `+08:00`。
 *
 *  按 ECMAScript 规范，带时间但无偏移的字符串要按**本地时间**解析。于是在
 *  UTC+8 下，所有时间显示都会比真实时间早 8 小时；凌晨 0–8 点创建的记录连
 *  日期都差一天（2026-11-03 01:00 UTC 会显示成 11-02）。
 *
 *  所以：缺时区信息就补 `Z`。已有 `Z` / `±HH:MM` 的原样信任（后端导出文件里
 *  是带 Z 的，别给它补成 `ZZ`）。
 */

/** 纯日期（无时间部分）—— 已经是"日历日"语义，按本地零点解析即可，
 *  不做时区换算，否则 UTC+8 下可能被推到前一天 */
const DATE_ONLY = /^\d{4}-\d{2}-\d{2}$/
const HAS_ZONE = /(?:Z|[+-]\d{2}:?\d{2})$/i

export function parseServerTime(value: string | null | undefined): Date | null {
  if (!value) return null
  const raw = String(value).trim()
  if (!raw) return null
  if (DATE_ONLY.test(raw)) {
    const date = new Date(`${raw}T00:00:00`)
    return Number.isNaN(date.getTime()) ? null : date
  }
  const normalized = HAS_ZONE.test(raw) ? raw : `${raw}Z`
  const date = new Date(normalized)
  return Number.isNaN(date.getTime()) ? null : date
}

/** 日期 + 时间（本地时区）。解析失败返回空串，不渲染 "Invalid Date" */
export function formatDateTime(value: string | null | undefined): string {
  const date = parseServerTime(value)
  return date ? date.toLocaleString('zh-CN') : ''
}

/** 只要时间部分（任务列表的"提交于 14:03"） */
export function formatTimeOnly(value: string | null | undefined): string {
  const date = parseServerTime(value)
  return date ? date.toLocaleTimeString('zh-CN') : ''
}

/** 只要日期（本地时区）。
 *
 *  注意：**不要**用 `value.slice(0, 10)` 或 `toISOString().slice(0, 10)` ——
 *  前者取的是 UTC 日期，后者更糟（先转 UTC 再截）。凌晨创建的记录会差一天。 */
export function formatDate(value: string | null | undefined): string {
  const date = parseServerTime(value)
  return date ? date.toLocaleDateString('zh-CN') : ''
}

/** 相对时间（"3 分钟前"）。超过 7 天退化成日期，避免"37 天前"这种没信息量的文案 */
export function formatRelative(value: string | null | undefined): string {
  const date = parseServerTime(value)
  if (!date) return ''
  const diffMs = Date.now() - date.getTime()
  const minutes = Math.floor(diffMs / 60000)
  if (minutes < 1) return '刚刚'
  if (minutes < 60) return `${minutes} 分钟前`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours} 小时前`
  const days = Math.floor(hours / 24)
  if (days <= 7) return `${days} 天前`
  return formatDate(value)
}
