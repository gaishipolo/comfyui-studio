/** 编辑器右栏 —— 子流程 / agent
 *
 * 子流程：别的应用能整个塞进当前应用当一步（它自己的输入按同名自动传）。
 * agent：一句话 → 草稿。模型只写配置，生成完是草稿，得试跑 + 人审批才上线。
 *
 * 这两件事放在同一栏，是因为它们都是「往流程里加一步，但这一步不是手搭的」。
 */
import { useEffect, useState } from 'react'
import type { AgentStatus, Flow } from '../../types'
import { notify } from '../../notify'

type RailTab = 'subflows' | 'agent'

export default function SideRail({
  flows,
  currentId,
  stepsUsingFlows,
  onAddSubflow,
  // ── agent ──
  agentStatus,
  agentOpenToken,
  agentBusy,
  agentErrors,
  requirement,
  onRequirementChange,
  onGenerate,
}: {
  flows: Flow[]
  /** 当前应用 —— 不能把自己当子流程 */
  currentId: string
  /** 已经作为子流程步骤引用的流程 id，用来标「已在流程里」 */
  stepsUsingFlows: string[]
  onAddSubflow: (flow: Flow) => void
  agentStatus: AgentStatus | null
  /** 外层每次想打开 agent 栏时递增，用来切到这一栏 */
  agentOpenToken: number
  agentBusy: boolean
  agentErrors: string[]
  requirement: string
  onRequirementChange: (text: string) => void
  onGenerate: () => void
}) {
  const [tab, setTab] = useState<RailTab>('subflows')

  // 外层点「一句话生成」时切到 agent 栏（token 变化 = 有人想让我打开它）
  useEffect(() => {
    if (agentOpenToken > 0) setTab('agent')
  }, [agentOpenToken])

  const candidates = flows.filter((f) => f.id !== currentId)

  return (
    <aside
      className="flex w-72 shrink-0 flex-col gap-xs overflow-hidden rounded-lg border border-tertiary bg-paper p-sm"
      data-app-rail="right"
    >
      <div className="flex rounded-lg border border-tertiary p-2xs" role="tablist">
        {(
          [
            ['subflows', '子流程'],
            ['agent', 'agent'],
          ] as [RailTab, string][]
        ).map(([key, label]) => (
          <button
            key={key}
            type="button"
            role="tab"
            aria-selected={tab === key}
            className={`flex-1 rounded px-sm py-2xs text-sm ${
              tab === key ? 'bg-secondary font-medium' : 'text-muted'
            }`}
            onClick={() => setTab(key)}
          >
            {label}
          </button>
        ))}
      </div>

      {tab === 'subflows' ? (
        <>
          <p className="text-xs text-muted">点一下把整个应用当成一步接进来</p>
          <ul className="flex min-h-0 flex-1 flex-col gap-2xs overflow-y-auto">
            {candidates.length === 0 ? (
              <li className="text-xs text-muted">还没有别的应用可以当子流程</li>
            ) : (
              candidates.map((f) => {
                const used = stepsUsingFlows.includes(f.id)
                return (
                  <li key={f.id}>
                    <button
                      type="button"
                      data-add-subflow={f.id}
                      className="w-full rounded-lg border border-tertiary p-sm text-left hover:border-rule-strong hover:bg-secondary"
                      onClick={() => {
                        if (used) {
                          notify(`「${f.name}」已经在流程里了`, 'info')
                          return
                        }
                        if (f.steps.some((s) => s.type === 'flow' && s.flow_id)) {
                          // 允许，但说一声 —— 嵌套三层以后很难排查
                          notify(`「${f.name}」自己也调用了子流程，注意别套太深`, 'warning')
                        }
                        onAddSubflow(f)
                      }}
                    >
                      <span className="block truncate text-sm">{f.name}</span>
                      <span className="mt-2xs block text-xs text-muted">
                        {f.steps.length} 步
                        {used ? ' · 已在流程里' : ''}
                        {f.status === 'draft' ? ' · 待审批' : ''}
                      </span>
                    </button>
                  </li>
                )
              })
            )}
          </ul>
        </>
      ) : (
        <div className="flex min-h-0 min-w-0 flex-1 flex-col gap-xs overflow-y-auto overflow-x-hidden">
          {agentStatus && !agentStatus.configured ? (
            <p className="rounded-md border border-warning-line bg-warning-soft px-sm py-xs text-xs text-warning">
              后端还没配置编排模型（LLM_BASE_URL / LLM_API_KEY / LLM_MODEL），暂时用不了。
              可以先用左边的节点手工搭。
            </p>
          ) : (
            <>
              <label className="field-label block">
                想做什么
                <textarea
                  className="field mt-2xs"
                  rows={4}
                  value={requirement}
                  disabled={agentBusy}
                  placeholder="例：先用 Flux2 生成一张拉布拉多拉拖拉机的图，再用图生图把拉布拉多换成边牧"
                  onChange={(e) => onRequirementChange(e.target.value)}
                />
              </label>
              <button
                type="button"
                className="btn btn-primary"
                disabled={agentBusy}
                onClick={onGenerate}
              >
                {agentBusy ? '正在生成…' : '生成草稿'}
              </button>
              <span className="text-xs text-muted">
                {agentBusy
                  ? '模型在想，通常 10~40 秒'
                  : agentStatus?.model
                    ? `模型：${agentStatus.model}`
                    : '生成后是草稿：先试跑看效果，再由人审批上线'}
              </span>
            </>
          )}

          {agentErrors.length > 0 && (
            <ul className="flex flex-col gap-2xs text-xs text-error">
              {agentErrors.map((msg, i) => (
                <li key={`${i}-${msg}`}>· {msg}</li>
              ))}
            </ul>
          )}
        </div>
      )}
    </aside>
  )
}
