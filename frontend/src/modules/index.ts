/**
 * 模块注册入口 — 所有功能模块在此注册
 *
 * 新增模块只需在本文件加 registerModule({...})，
 * App 自动渲染对应 Tab，无需修改 App.tsx。
 */
import { registerModule } from './registry'
import TaskPanel from '../components/TaskPanel'
import AssetGallery from '../components/AssetGallery'
import TemplateManager from '../components/TemplateManager'
import GenerateWorkspace from '../components/GenerateWorkspace'
import ApprovalModule from '../components/modules/ApprovalModule'
import DashboardModule from '../components/modules/DashboardModule'
import UserManager from '../components/modules/UserManager'
import UsagePanel from '../components/modules/UsagePanel'
import PluginManager from '../components/modules/PluginManager'

// ── 核心模块 ────────────────────────────────
// 组件按需接收 ModuleProps 的子集即可（结构化类型兼容，无需类型断言）

registerModule({
  key: 'generate',
  label: '生成',
  component: GenerateWorkspace,
  order: 1,
})

registerModule({
  key: 'tasks',
  label: '任务',
  component: TaskPanel,
  order: 2,
})

registerModule({
  key: 'assets',
  label: '素材库',
  component: AssetGallery,
  order: 3,
})

registerModule({
  key: 'admin',
  label: '管理',
  component: TemplateManager,
  roles: ['admin'], // 仅 admin 可见
  order: 4,
})

registerModule({
  key: 'users',
  label: '用户',
  component: UserManager,
  roles: ['admin'], // 账号管理只给管理员
  order: 5,
})

registerModule({
  key: 'usage',
  label: '用量统计',
  component: UsagePanel,
  roles: ['admin'], // 只统计不拦截，仅 admin 看得到
  order: 6,
})

registerModule({
  key: 'plugins',
  label: '引擎插件',
  component: PluginManager,
  roles: ['admin'], // 上传=执行代码，只给管理员（=开发）
  order: 7,
})

// ── 预留模块（示例：审批流，当前禁用）────────

registerModule({
  key: 'approval',
  label: '审批',
  component: ApprovalModule,
  enabled: false, // 上线时改为 true 即可，无需改代码
  order: 6,
})

// ── 预留模块（示例：数据看板，当前禁用）──────

registerModule({
  key: 'dashboard',
  label: '看板',
  component: DashboardModule,
  roles: ['admin'],
  enabled: false,
  order: 7,
})

export { getVisibleModules, registerModule, enableModule, unregisterModule } from './registry'
