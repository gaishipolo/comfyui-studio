/** 审批模块（预留占位）：启用方式是在 modules/index.ts 中设 enabled: true */
export default function ApprovalModule() {
  return (
    <section>
      <h2 className="rule-b pb-sm font-display text-section">审批模块</h2>
      <div className="mt-lg border border-dashed border-rule px-md py-xl">
        <p className="font-display text-title text-ink">内容审批流占位模块</p>
        <p className="mt-xs max-w-measure text-base text-ink-2">
          上线时在 <code className="code">modules/index.ts</code> 中将 enabled 改为 true
          即可启用，无需修改其他代码。
        </p>
      </div>
    </section>
  )
}
