/** 引擎报错的「人话」翻译 —— 运营看处置建议，技术人员展开看原文
 *
 * 背景（P1-1）：任务失败时 error_msg 是引擎原样透传，
 * `adaln kernel launch failed: invalid argument` 给运营看等于没说。
 * 这里按已知错误模式给出处置建议；匹配不上的原样展示，不做瞎猜。 */

const RULES: { pattern: RegExp; text: string }[] = [
  {
    pattern: /adaln kernel launch failed/i,
    text: '生成引擎内部计算出错（大负载下的已知问题）。建议：降低分辨率或帧数后重试；反复出现请联系管理员重启生成服务',
  },
  {
    pattern: /cuda out of memory|out of memory|hip out of memory/i,
    text: '生成服务显存不足。建议：降低分辨率或减少生成数量，稍等片刻再试',
  },
  {
    pattern: /invalid_input_type|invalid prompt|value not in list|return type mismatch|failed validation/i,
    text: '生成参数不被引擎接受。请检查输入值（尤其下拉选项和数字）后重试',
  },
  {
    pattern: /timed? ?out|timeout/i,
    text: '生成超时（任务太重或引擎繁忙）。可稍后重试，或降低分辨率/帧数',
  },
  {
    pattern: /connection|connect\b|econnrefused|network|unreachable/i,
    text: '连不上生成服务。稍后重试；持续失败请联系管理员',
  },
  {
    pattern: /no such file|checkpoint|safetensors|model.*not found/i,
    text: '生成引擎缺少所需模型文件。请联系管理员安装对应模型',
  },
  {
    pattern: /interrupt|cancel/i,
    text: '任务已被中断',
  },
]

/** 把引擎报错翻译成处置建议。匹配不上时原样返回，matched=false */
export function friendlyError(raw: string): { text: string; raw: string; matched: boolean } {
  for (const { pattern, text } of RULES) {
    if (pattern.test(raw)) return { text, raw, matched: true }
  }
  return { text: raw, raw, matched: false }
}

/** 失败信息展示块：主文是人话，匹配到已知模式时可展开原文 */
export default function FriendlyError({ message }: { message: string }) {
  const fe = friendlyError(message)
  return (
    <div className="mt-xs border border-error bg-paper px-sm py-xs text-sm text-error">
      {fe.text}
      {fe.matched && (
        <details className="mt-2xs">
          <summary className="cursor-pointer text-xs text-muted">原始报错</summary>
          <pre className="mt-2xs overflow-x-auto whitespace-pre-wrap break-all text-xs text-muted">
            {fe.raw}
          </pre>
        </details>
      )}
    </div>
  )
}
