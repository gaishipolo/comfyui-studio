/** 列表视图的三态判定：loading / error / empty / list。
 *
 * 网络错误与「真的没有数据」必须分开 —— 加载失败静默 setAssets([]) 会让用户
 * 以为素材没了。调用方传 failed 标记，这里给出该渲染哪个状态。 */
export type ListViewState = 'loading' | 'error' | 'empty' | 'list'

export function assetListViewState(input: {
  loading: boolean
  failed: boolean
  count: number
}): ListViewState {
  if (input.loading) return 'loading'
  if (input.count > 0) return 'list'
  return input.failed ? 'error' : 'empty'
}

export function createLatestRequestGate() {
  let latest = 0
  return {
    start(): number {
      latest += 1
      return latest
    },
    isLatest(request: number): boolean {
      return request === latest
    },
  }
}

export function taskListViewState(input: {
  failed: boolean
  count: number
}): Extract<ListViewState, 'error' | 'empty' | 'list'> {
  if (input.count > 0) return 'list'
  return input.failed ? 'error' : 'empty'
}
