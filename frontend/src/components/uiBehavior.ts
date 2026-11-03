type TaskLike = { status: string }
type FieldLike = { name: string; type: string; required?: boolean; multiple?: boolean }

/** 「随机数」字段的默认取值范围。
 *
 *  上界取 2^31-1 而不是 2^64-1：后者超过 Number.MAX_SAFE_INTEGER，
 *  经过 JSON 往返会悄悄丢精度（末位变 0），种子「看起来变了其实重复」。
 *  需要更大范围时在字段上显式写 max。 */
export const DEFAULT_RANDOM_MAX = 2_147_483_647

/** 「每次都换一个」的哨兵值。填 -1 时由后端在提交那一刻展开成真实随机数，
 *  批量生成里每个任务各取一个；展开后的值会写回任务参数，所以事后还能复现。 */
export const RANDOM_SENTINEL = -1

/** 随机数输入框的 min 属性。
 *
 *  不能直接用字段下界：下界默认是 0，浏览器原生校验会拦下 -1
 *  （「值必须大于或等于 0」），用户就永远填不进「自动随机」。
 *  取两者更小值，既保留正常范围提示，也让 -1 可以输入和提交。 */
export function randomFieldInputMin(low: number | undefined): number {
  return Math.min(RANDOM_SENTINEL, Number.isFinite(low) ? (low as number) : 0)
}

/** 随机数字段的初始值。
 *
 *  模板里的 `default` 常常是字符串（编辑器里就是个文本框，存量模板还留着
 *  "-1" 这种写法）。原样塞进表单的话，提交出去是字符串，后端认不出 -1
 *  哨兵，等于「每次都随机」静默失效。这里统一解析成数字，解析不出来
 *  （空串、乱码）才现取一个。
 */
export function initialRandomValue(
  raw: unknown,
  low: number,
  high: number,
): number {
  const text = typeof raw === 'string' ? raw.trim() : raw
  if (text !== '' && text !== undefined && text !== null) {
    // 只认数字与数字字符串：布尔值不能靠 Number(true)=1 混进来
    const parsed =
      typeof text === 'number' ? text : typeof text === 'string' ? Number(text) : Number.NaN
    if (Number.isFinite(parsed)) return parsed
  }
  return randomInt(low, high)
}

/** 取 [min, max] 闭区间的随机整数。
 *
 *  边界归一化集中在这里，方便离线断言：传反了自动交换，非有限值退回默认，
 *  小数按内向外取整（min 向上、max 向下），保证返回值一定是区间内的整数。
 */
export function randomInt(min: number, max: number): number {
  let lo = Number.isFinite(min) ? Math.ceil(min) : 0
  let hi = Number.isFinite(max) ? Math.floor(max) : DEFAULT_RANDOM_MAX
  if (lo > hi) [lo, hi] = [hi, lo]
  const span = Math.min(hi - lo + 1, Number.MAX_SAFE_INTEGER)
  return lo + Math.floor(Math.random() * span)
}

export function taskPollDelay(tasks: TaskLike[]): number {
  return tasks.some((task) => task.status === 'pending' || task.status === 'running')
    ? 3_000
    : 15_000
}

export function localMonthStart(offset: number, now: Date = new Date()): string {
  const date = new Date(now.getFullYear(), now.getMonth() + offset, 1)
  return localDateString(date)
}

/** 某个月的最后一天（本地时区）。offset = -1 即上月最后一天。
 *
 *  日期区间是**闭区间**（后端把纯日期解成当天 23:59:59），所以"上月"的
 *  dateTo 必须是上月最后一天 —— 用本月 1 号当上界会把本月 1 号的数据
 *  同时算进上个月。 */
export function localMonthEnd(offset: number, now: Date = new Date()): string {
  // 下个月的第 0 天 = 这个月的最后一天
  const date = new Date(now.getFullYear(), now.getMonth() + offset + 1, 0)
  return localDateString(date)
}

function localDateString(date: Date): string {
  const year = date.getFullYear()
  const month = String(date.getMonth() + 1).padStart(2, '0')
  const day = String(date.getDate()).padStart(2, '0')
  return `${year}-${month}-${day}`
}

export function missingRequiredUpload(
  fields: FieldLike[],
  values: Record<string, unknown>,
): string | null {
  for (const field of fields) {
    if (!field.required || (field.type !== 'file_upload' && field.type !== 'image_upload')) continue
    const value = values[field.name]
    if (value === '' || value === null || value === undefined) return field.name
    if (Array.isArray(value) && value.length === 0) return field.name
  }
  return null
}

export function missingRequiredList(
  fields: FieldLike[],
  values: Record<string, unknown>,
): string | null {
  for (const field of fields) {
    if (
      !field.required
      || !field.multiple
      || field.type === 'file_upload'
      || field.type === 'image_upload'
    ) continue
    const value = values[field.name]
    if (!Array.isArray(value)) return field.name
    const meaningful = value.some((item) => item !== '' && item !== null && item !== undefined)
    if (!meaningful) return field.name
  }
  return null
}

export function normalizeBatchCount(value: number): number {
  if (!Number.isFinite(value)) return 1
  return Math.max(1, Math.min(32, Math.round(value)))
}

export function clearTaskStatusHistory(history: Map<string, string>): void {
  history.clear()
}
