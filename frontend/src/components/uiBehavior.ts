type TaskLike = { status: string }
type FieldLike = { name: string; type: string; required?: boolean; multiple?: boolean }

export function taskPollDelay(tasks: TaskLike[]): number {
  return tasks.some((task) => task.status === 'pending' || task.status === 'running')
    ? 3_000
    : 15_000
}

export function localMonthStart(offset: number, now: Date = new Date()): string {
  const date = new Date(now.getFullYear(), now.getMonth() + offset, 1)
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
