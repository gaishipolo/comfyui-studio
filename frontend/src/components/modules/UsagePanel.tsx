/** 用量统计（仅 admin）— 谁用了多少，看清楚就够了，不拦人
 *
 *  背景（P2）：多用户跑起来之后，admin 需要一眼看到每人生成了多少、占了多少存储、
 *  平均一次要等多久。**只统计不拦截** —— 超不超额都不影响正常提交，避免误伤业务。
 *
 *  口径与后端一致：
 *  - 任务数按状态分列（成功/失败/取消）
 *  - 平均耗时只算已完成任务（finished_at - started_at），没完成任务显示 —
 *  - 存储按素材字节数累加，展示时自动换算 KB/MB/GB
 *  - 时间区间语义同素材筛选（纯日期含当天全天） */
import { useCallback, useEffect, useState } from 'react'
import type { UsageResponse } from '../../types'
import { getUsage } from '../../api/client'
import type { ModuleProps } from '../../modules/registry'
import { localMonthStart } from '../uiBehavior'

const formatSize = (bytes: number) =>
  bytes > 1024 * 1024 * 1024
    ? `${(bytes / 1024 / 1024 / 1024).toFixed(2)} GB`
    : bytes > 1024 * 1024
      ? `${(bytes / 1024 / 1024).toFixed(1)} MB`
      : `${(bytes / 1024).toFixed(0)} KB`

/** 秒数 → 「3 分 20 秒」/「45 秒」/「—」 */
const formatDuration = (s: number | null) => {
  if (s === null || s === undefined) return '—'
  if (s < 60) return `${s.toFixed(0)} 秒`
  const m = Math.floor(s / 60)
  const rest = Math.round(s % 60)
  return `${m} 分 ${rest} 秒`
}

export default function UsagePanel({ user }: ModuleProps) {
  const [dateFrom, setDateFrom] = useState('')
  const [dateTo, setDateTo] = useState('')
  const [data, setData] = useState<UsageResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const res = await getUsage(dateFrom || undefined, dateTo || undefined)
      setData(res)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setLoading(false)
    }
  }, [dateFrom, dateTo])

  useEffect(() => {
    if (user.role !== 'admin') return
    load()
  }, [load, user.role])

  // 普通用户点进来（理论上 Tab 都看不见，双保险）—— 后端也会 403
  if (user.role !== 'admin') {
    return <p className="py-xl text-center text-base text-muted">仅管理员可查看用量统计</p>
  }

  return (
    <section>
      <div className="page-head">
        <div>
          <h2 className="page-title">用量统计</h2>
          <p className="page-sub">按用户汇总任务、素材与存储占用</p>
        </div>
        <div className="flex flex-wrap items-center gap-xs">
          <input
            type="date"
            className="field w-auto"
            aria-label="统计起始日期"
            title="起始日期（含当天）"
            value={dateFrom}
            onChange={(e) => setDateFrom(e.target.value)}
          />
          <span className="text-sm text-muted">至</span>
          <input
            type="date"
            className="field w-auto"
            aria-label="统计截止日期"
            title="截止日期（含当天全天）"
            value={dateTo}
            onChange={(e) => setDateTo(e.target.value)}
          />
          <button
            className="btn"
            onClick={() => {
              setDateFrom(localMonthStart(0))
              setDateTo('')
            }}
          >
            本月
          </button>
          <button
            className="btn"
            onClick={() => {
              setDateFrom(localMonthStart(-1))
              setDateTo(localMonthStart(0))
            }}
          >
            上月
          </button>
          <button
            className="btn"
            onClick={() => {
              setDateFrom('')
              setDateTo('')
            }}
          >
            全部
          </button>
        </div>
      </div>

      {error && (
        <p className="mb-md rounded-sm border border-error-line bg-error-soft px-sm py-xs text-sm text-error">
          加载失败：{error}
        </p>
      )}

      {loading && !data ? (
        <div className="rounded-md border border-rule bg-paper p-md shadow-xs" aria-hidden="true">
          <div className="space-y-sm">
            {Array.from({ length: 5 }).map((_, i) => (
              <div key={i} className="skeleton h-md" />
            ))}
          </div>
        </div>
      ) : data ? (
        <>
          {/* 当前区间提示 —— 「全部」时不显示（默认就是不限） */}
          {(data.date_from || data.date_to) && (
            <p className="mt-xs text-sm text-muted">
              统计区间：{data.date_from || '不限'} 至 {data.date_to || '不限'}
            </p>
          )}

          {data.by_user.length === 0 ? (
            <div className="empty">
              <span className="empty-icon" aria-hidden="true">
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none">
                  <path
                    d="M4 19V9M10 19V5M16 19v-6M22 19H2"
                    stroke="currentColor"
                    strokeWidth="1.7"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                  />
                </svg>
              </span>
              <p className="empty-title">该时间段内没有任务记录</p>
              <p className="empty-hint">换个时间区间再看看，或点「全部」查看历史累计</p>
            </div>
          ) : (
            <>
              {/* 摘要卡：先给结论，再给明细表。数字一眼能读，明细留给需要核对的人 */}
              <div className="mb-md grid gap-sm sm:grid-cols-2 xl:grid-cols-4">
                <div className="stat">
                  <p className="stat-label">任务总数</p>
                  <p className="stat-value">{data.totals.task_total}</p>
                  <p className="stat-sub">
                    成功 {data.totals.task_done} · 失败 {data.totals.task_error} · 取消{' '}
                    {data.totals.task_cancelled}
                  </p>
                </div>
                <div className="stat">
                  <p className="stat-label">参与用户</p>
                  <p className="stat-value">{data.by_user.length}</p>
                  <p className="stat-sub">该区间内提交过任务的账号</p>
                </div>
                <div className="stat">
                  <p className="stat-label">素材与存储</p>
                  <p className="stat-value">{data.totals.asset_count}</p>
                  <p className="stat-sub">共占用 {formatSize(data.totals.storage_bytes)}</p>
                </div>
                <div className="stat">
                  <p className="stat-label">平均耗时</p>
                  <p className="stat-value">{formatDuration(data.totals.avg_duration_seconds)}</p>
                  <p className="stat-sub">仅统计已完成的任务</p>
                </div>
              </div>

              <div className="overflow-hidden rounded-md border border-rule bg-paper shadow-xs">
                <table className="table">
                  <caption className="visually-hidden">用户用量统计</caption>
                  <thead>
                    <tr>
                      <th scope="col" className="w-[13rem]">用户</th>
                      <th scope="col" className="num">任务数</th>
                      <th scope="col" className="num">成功</th>
                      <th scope="col" className="num">失败</th>
                      <th scope="col" className="num">取消</th>
                      <th scope="col" className="num">素材数</th>
                      <th scope="col" className="num">存储占用</th>
                      <th scope="col" className="num">平均耗时</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.by_user.map((u) => (
                      <tr key={u.user_id || '__none__'}>
                        <td>
                          {u.display_name || u.username}
                          {u.role === 'admin' && <span className="ml-2xs chip">管理员</span>}
                        </td>
                        <td className="num">{u.task_total}</td>
                        <td className="num">{u.task_done}</td>
                        <td className="num">{u.task_error}</td>
                        <td className="num">{u.task_cancelled}</td>
                        <td className="num">{u.asset_count}</td>
                        <td className="num">{formatSize(u.storage_bytes)}</td>
                        <td className="num">{formatDuration(u.avg_duration_seconds)}</td>
                      </tr>
                    ))}
                    {/* 合计行：直接展示后端算好的总数，不做前端二次相加 */}
                    <tr className="bg-paper-2 hover:bg-paper-2">
                      <td>
                        <strong>{data.totals.username}</strong>
                      </td>
                      <td className="num">
                        <strong>{data.totals.task_total}</strong>
                      </td>
                      <td className="num">
                        <strong>{data.totals.task_done}</strong>
                      </td>
                      <td className="num">
                        <strong>{data.totals.task_error}</strong>
                      </td>
                      <td className="num">
                        <strong>{data.totals.task_cancelled}</strong>
                      </td>
                      <td className="num">
                        <strong>{data.totals.asset_count}</strong>
                      </td>
                      <td className="num">
                        <strong>{formatSize(data.totals.storage_bytes)}</strong>
                      </td>
                      <td className="num">
                        <strong>{formatDuration(data.totals.avg_duration_seconds)}</strong>
                      </td>
                    </tr>
                  </tbody>
                </table>
              </div>
            </>
          )}
        </>
      ) : null}
    </section>
  )
}
