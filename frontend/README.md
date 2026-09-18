# 管理后台前端

旅游攻略多 Agent 项目的内部管理界面。当前实现使用 React 19、TypeScript、Vite、Tailwind CSS、React Router、TanStack Query、Recharts 和 Vitest，不是 Vite 初始化模板。

## 当前页面

- Project 列表、搜索、状态筛选、软删除与回收站；
- Project 概览、Agent 运行状态和完整时间线；
- Agent 产出、POI 照片和用户备注管理；
- 素材库、素材详情和跨来源洞察；
- 静态网站与海报生成、预览；
- full / light / mimo 等模型配置和 key 池运维；
- 飞书绑定、健康状态、推送日志和活跃度；
- 跨项目定向复核中心、证据查看、模型建议和逐条人工决定。

复核中心已接入 QueueGroup、后端 finalize 和 Patch diff，可应用、拒绝并为已应用 Patch 生成回滚修改。人工“采纳”仍不会直接改正文：只有明确应用 Patch 才会写入业务文件。人工修改值使用类型安全的结构化编辑器，对象按字段展开、字符串数组按条目编辑；独立建议历史面板可只读查看当前、已替代和候选 recommendation 及其人工决定进度。

## 本地运行

先在仓库根目录启动 FastAPI：

```bash
uvicorn src.api.app:app --reload --port 28000
```

再启动前端：

```bash
cd frontend
npm install
npm run dev
```

Vite 默认监听 `18888`，并把 `/api` 代理到 `http://localhost:28000`，配置见 `vite.config.ts`。

## 验证

```bash
npm run test
npm run lint
npm run build
```

组件测试主要使用 jsdom 和 mock API；通过测试和构建不代表真实后端数据、浏览器视觉效果或外部服务已经完成端到端验证。

## 代码入口

- `src/App.tsx`：页面路由；
- `src/api/`：类型、请求客户端和 TanStack Query hooks；
- `src/pages/`：业务页面；
- `src/components/`：共享布局和交互组件；
- `src/lib/status.ts`：项目及 Agent 状态展示映射。

完整后端启动、环境变量和验证边界见 `docs/DEV_SETUP.md`；产品范围和接口边界见 `docs/ARCHITECTURE.md` 第 13.2、14 和 22 节。
