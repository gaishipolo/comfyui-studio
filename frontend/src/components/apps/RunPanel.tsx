/** 运行台 —— 一次流程运行的进度与每一步的状态
 *
 * 从旧的「流程」页抽出来的纯展示组件：不取数、不管轮询，只把传进来的
 * `run` 画出来。这样它既能给应用编辑器用，也能放到别处（比如任务详情）。
 *
 * 三件事值得留着注释：
 * 1. 进度只在这里 clamp 一次 —— 后端已经夹过，但一条脏数据不该让进度条
 *    冲出轨道、也不该显示成 "150%"
 * 2. 循环步骤一次运行有 N 行（同一 step_id），key 必须带实例序号，
 *    否则 React 会复用错行
 * 3. 子流程步骤没有 template_id，没填标题时要退回子流程名，不是「未知节点」
 */
import type { Flow, FlowRun, FlowStep, NodeCapability } from '../../types'

const RUN_STATUS_LABEL: Record<string, string> = {
  running: '运行中',
  done: '已完成',
  error: '失败',
  cancelled: '已取消',
}

const TASK_STATUS_LABEL: Record<string, string> = {
  pending: '排队中',
  running: '生成中',
  done: '已完成',
  error: '失败',
  cancelled: '已取消',
}

export default function RunPanel({
  run,
  steps,
  caps,
  flows,
  onCancel,
}: {
  run: FlowRun
  /** 这次运行所属流程的步骤定义 —— 用来把 step_id 还原成「生成参考图」 */
  steps: FlowStep[]
  caps: NodeCapability[]
  /** 全部流程 —— 子流程步骤要显示它调用的流程名 */
  flows: Flow[]
  onCancel?: () => void
}) {
  const nodeName = (templateId: string) =>
    caps.find((c) => c.id === templateId)?.name ?? '未知节点'
  const pct = Math.max(0, Math.min(100, run.progress ?? 0))

  return (
    <div className="panel" data-run-panel>
      <div className="flex flex-wrap items-center justify-between gap-sm">
        <h4 className="font-display text-base font-medium">运行 {run.flow_name}</h4>
        <div className="flex items-center gap-xs">
          <span
            role="progressbar"
            data-run-progress
            aria-label="流程进度"
            aria-valuenow={Math.round(pct)}
            aria-valuemin={0}
            aria-valuemax={100}
            className="h-1.5 w-16 overflow-hidden rounded-pill bg-paper-3"
          >
            <span
              className={`block h-full rounded-pill ${
                run.status === 'error'
                  ? 'bg-error'
                  : run.status === 'cancelled'
                    ? 'bg-muted'
                    : 'bg-accent'
              }`}
              style={{ width: `${pct}%` }}
            />
          </span>
          <span data-run-percent className="w-9 text-right text-sm text-ink-2">
            {Math.round(pct)}%
          </span>
          <span data-run-status className="chip">
            {RUN_STATUS_LABEL[run.status] ?? run.status}
          </span>
          {run.status === 'running' && onCancel && (
            <button type="button" className="btn btn-quiet" data-run-cancel onClick={onCancel}>
              取消
            </button>
          )}
        </div>
      </div>

      {run.error_msg && <p className="mt-2xs text-sm text-error">{run.error_msg}</p>}

      <ol data-run-steps className="mt-sm flex flex-col gap-2xs">
        {run.steps.map((s, i) => {
          // 循环步骤一次运行会有 N 行（同一 step_id）—— key 只用 step_id
          // 会重复，React 会复用错行。嵌套子流程的 task_id 为空，也不能拿来当 key。
          const stepDef = steps.find((st) => st.id === s.step_id)
          return (
            <li
              key={`${s.step_id}-${s.iteration ?? 0}-${i}`}
              className="flex items-center gap-sm text-sm"
            >
              <span className="chip">{i + 1}</span>
              {typeof s.layer === 'number' && (
                <span className="text-xs text-muted">第 {s.layer + 1} 层</span>
              )}
              <span>
                {stepDef?.title ||
                  (stepDef?.type === 'flow'
                    ? (flows.find((f) => f.id === stepDef.flow_id)?.name ?? '子流程')
                    : nodeName(stepDef?.template_id ?? ''))}
              </span>
              {stepDef?.type === 'repeat' && typeof s.iteration === 'number' && (
                <span className="text-xs text-muted">第 {s.iteration + 1} 次</span>
              )}
              <span
                className={
                  s.status === 'done'
                    ? 'text-muted'
                    : s.status === 'error'
                      ? 'text-error'
                      : 'text-ink-2'
                }
              >
                {TASK_STATUS_LABEL[s.status] ?? s.status}
              </span>
              {s.outputs && s.outputs.length > 0 && (
                <span className="text-xs text-muted">{s.outputs.length} 个产物</span>
              )}
            </li>
          )
        })}
        {run.status === 'running' && run.steps.length === 0 && (
          <li className="text-sm text-muted">正在执行第一步…</li>
        )}
      </ol>

      {run.status === 'done' && run.outputs?.length > 0 && (
        <>
          <h5 className="mt-md font-display text-sm font-medium">流程产出</h5>
          <div className="mt-2xs flex flex-col gap-2xs">
            {run.outputs.map((o) => (
              <div key={o.name} className="flex items-center gap-sm text-sm">
                <span className="chip">{o.name}</span>
                <span className="text-xs text-muted">{o.outputs.length} 个产物</span>
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  )
}
