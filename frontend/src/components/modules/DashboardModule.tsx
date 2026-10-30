/** 数据看板模块（预留占位）：启用方式是在 modules/index.ts 中设 enabled: true */
export default function DashboardModule() {
  return (
    <section>
      <h2 className="rule-b pb-sm font-display text-section">数据看板</h2>
      <div className="mt-lg border border-dashed border-rule px-md py-xl">
        <p className="font-display text-title text-ink">生成量统计、任务成功率、素材趋势</p>
        <p className="mt-xs max-w-measure text-base text-ink-2">
          上线时在 <code className="code">modules/index.ts</code> 中将 enabled 改为 true
          即可启用。
        </p>
      </div>
    </section>
  )
}
