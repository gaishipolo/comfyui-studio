import js from '@eslint/js'
import tseslint from 'typescript-eslint'
import reactHooks from 'eslint-plugin-react-hooks'
import globals from 'globals'

/**
 * eslint 配置 —— 和 backend/ruff.toml 同一个原则：
 * 进 CI 的规则必须做到「报出来的每一条都值得改」。
 *
 * 因此没有直接展开 react-hooks 的 recommended（v7 里它带 16 条规则，
 * 大半是面向 React 19 Compiler 的 purity / immutability / refs 之类），
 * 只显式启用两条：
 *   - rules-of-hooks：hooks 写在条件或循环里会直接让组件崩，必须拦
 *   - exhaustive-deps：依赖数组漏项会读到过期闭包，这是 React 里最高频的真 bug
 * 刻意不开 set-state-in-effect：本项目 14 处命中基本都是「effect 里加载数据后
 * setState」这一惯用模式，按它改要重构数据加载链路，收益小于风险。
 */
export default tseslint.config(
  {
    ignores: ['dist/**', 'node_modules/**'],
  },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ['src/**/*.{ts,tsx}'],
    plugins: { 'react-hooks': reactHooks },
    languageOptions: {
      globals: { ...globals.browser },
    },
    rules: {
      'react-hooks/rules-of-hooks': 'error',
      'react-hooks/exhaustive-deps': 'error',

      // tsc 的 strict 已经挡住了大部分，这里补的是「类型上合法但没意义」的写法
      '@typescript-eslint/no-explicit-any': 'error',
      '@typescript-eslint/no-unused-vars': [
        'error',
        { argsIgnorePattern: '^_', varsIgnorePattern: '^_', caughtErrors: 'none' },
      ],
    },
  },
)
