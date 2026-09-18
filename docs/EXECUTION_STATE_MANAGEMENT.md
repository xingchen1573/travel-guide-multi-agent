# 轻量级执行状态管理

> 项目列表口径：`projects/` 下只有存在 `workspace/progress.json` 的正式项目才进入列表
> （工作区归档重建期间若存在 SQLite 活动任务则继续保留）。列表中的“正在执行”只统计
> SQLite 最新任务状态为 `queued/running` 的项目；项目阶段尚未完成但没有活动任务时属于
> “未完成”，不能据此推断后台线程仍在运行。陈旧 `run_status.json` 只作为展示投影为
> `interrupted`，不参与“正在执行”计数。

## 1. 问题背景

旧管理后台把后台任务状态保存在 `src/api/jobs.py::_jobs`，进程退出后记录消失；Agent 节点状态则写入 `workspace/run_status.json`，历史日志和复核运行文件也可能长期保留未完成外观。结果是：进程重启后无法判断旧线程是否仍存在，缺少结束事件会被误读为持续运行，迟到线程还可能覆盖取消或人工收口后的终态。

当前实现面向个人使用和小规模自部署，不引入 Redis、Celery、Temporal 或新常驻服务。新增的全局执行库默认位于 `runtime/execution_state.sqlite3`，沿用 Python 标准库 `sqlite3`、FastAPI lifespan 和现有后台线程。

## 2. 状态模型

生命周期状态：

- `queued`：任务已登记，执行线程尚未绑定 owner。
- `running`：线程已真正开始并绑定 `owner_instance_id`。
- `waiting_user`：HITL、审批或外部任务包等待用户处理，不表示后台正在消耗执行资源。
- `succeeded`、`failed`、`cancelled`、`interrupted`：终态。

健康状态独立于生命周期：

- `healthy`：owner 心跳正常，业务进度未超过停滞阈值。
- `stalled`：任务仍为 `running`，owner 存活，但业务进度超过 15 分钟未变化。首期只展示和筛选，不自动终止。
- `orphaned`：owner 不存在、已停止或心跳过期，任务已收口为 `interrupted`。

所有进度、步骤、成功和失败更新都带 `WHERE status IN ('queued', 'running')` 条件。已经进入 `succeeded`、`failed`、`cancelled` 或 `interrupted` 的任务不会被迟到线程覆盖。

## 3. SQLite 表和字段

### `runtime_instances`

字段为 `instance_id`、`role`、`environment`、`pid`、`started_at`、`heartbeat_at`、`stopped_at`。backend 和独立飞书 worker 共用注册/心跳实现；只有 backend 是协调者。

### `jobs`

核心字段包括：

- 标识：`job_id`、`task_type`、`task_key`
- 生命周期：`status`、`status_reason`、`created_at`、`updated_at`、`started_at`、`finished_at`
- owner/活性：`owner_instance_id`、`last_heartbeat_at`、`last_progress_at`、`health_status`
- 运行上下文：`environment`、`attempt`、`retry_of_job_id`
- 当前结果投影：`result_json`、`error_json`、`log_json`

### `job_steps`

保存 `job_id`、`step_key`、步骤状态和时间。任务被协调为 `interrupted` 时，仍为 `running` 的步骤同步收口。

`JobService.ensure_schema()` 使用 `CREATE TABLE IF NOT EXISTS` 和 `PRAGMA table_info` + `ALTER TABLE ADD COLUMN` 做增量兼容，不删除已有任务、结果或 artifact。每个操作使用独立 SQLite 连接，启用 WAL、foreign keys 和 busy timeout；状态迁移使用短事务，不共享跨线程连接。

## 4. FastAPI lifespan 与维护线程

`src/api/app.py::create_app` 通过 lifespan 创建 `RuntimeManager`：

1. 启动时注册 `role=backend` 实例。
2. 立即协调一次活动任务。
3. 启动 daemon 维护线程。
4. 每 15 秒刷新实例心跳，并只刷新该 owner 活动任务的 `last_heartbeat_at`。
5. 每 60 秒协调一次。
6. 关闭时设置 stop event、join 线程、写 `stopped_at`，再做一次最终协调。

pytest 默认关闭 app 自动维护线程；测试通过 `create_app(runtime_enabled=True, job_service=临时库)` 或直接创建 `RuntimeManager(enabled=True)` 显式验证线程。`tests/conftest.py` 为每个测试提供临时 SQLite，避免扫描个人数据库。

`run_feishu_worker.py` 注册 `role=feishu_worker` 并维护心跳，但 `coordinator=False`，避免多个进程重复扫描。

## 5. 心跳、进度和 stalled

- `heartbeat_at` / `last_heartbeat_at` 只证明进程还活着。
- `last_progress_at` 只在业务日志、步骤或结果进展时更新。
- 实例心跳绝不刷新 `last_progress_at`。
- owner 心跳正常但 `last_progress_at` 超过 15 分钟时，只把 `health_status` 设为 `stalled`，生命周期仍为 `running`。

## 6. 协调算法

协调器扫描 `queued/running` 后先在内存中分类，再执行写入：

1. 收集中断候选和 `healthy/stalled` 健康更新。
2. 逐个调用 `JobService.interrupt_job()`，每个任务独立提交。
3. 另起短事务批量更新仍处于活动状态的健康字段。

中断条件：owner 记录不存在、owner 有 `stopped_at`、owner 心跳超过 60 秒，或无 owner 的升级前任务最后更新时间超过 24 小时。中断写入结构化 `retryable=true` 错误、`health_status=orphaned`、`finished_at` 和明确原因。所有更新都再次检查任务仍处于活动状态，因此重复协调幂等。

## 7. 观测状态

`src/observability/execution.py` 只从事件或 `run_status.json` 生成展示投影：

- 明确完成/错误证据映射为 `succeeded/failed`。
- HITL、审批和 `await_*` 节点映射为 `waiting_user`。
- `received/recorded/dispatched/delivered` 等壳事件不能单独证明 `running`。
- 未完成执行超过 15 分钟没有近期业务活动证据时显示 `interrupted`。
- 历史文件本身不被改写。

管理后台任务 API 从 SQLite 读取状态；`run_status.json` 只保留为 Agent 节点观测快照。

## 8. 前端行为

前端类型、徽标和复核运行筛选支持：

- `interrupted`：已中断，终态，停止轮询。
- `waiting_user`：等你处理，不显示成后台运行。
- `running + stalled`：进展停滞，保留 running 生命周期。

通用任务轮询只在 `queued/running` 时继续；复核运行只在 `queued/running/validating` 时继续；Agent 快照只在 `running` 时继续。

## 9. Docker 职责边界

截至 2026-08-27，仓库内没有 Dockerfile 或 Compose 文件，因此本次没有新增一个无法独立工作的部署清单。无论未来使用 Docker restart policy、systemd 还是其他监督器，边界保持不变：

- 维护线程随着 FastAPI 进程运行，只能在后端存活时扫描 SQLite。
- 它不能保证 FastAPI 自身永远运行。
- FastAPI 退出后的重新拉起由 Docker、systemd 或其他外部监督机制负责。
- Docker 只负责部署和进程拉起，不根据业务日志推断任务状态。

## 10. 默认参数

| 参数 | 默认值 |
|---|---:|
| 实例心跳间隔 | 15 秒 |
| 协调扫描间隔 | 60 秒 |
| owner 心跳失联阈值 | 60 秒 |
| 无业务进度 stalled 阈值 | 15 分钟 |
| 无 owner 历史任务宽限期 | 24 小时 |
| 观测近期活动窗口 | 15 分钟 |

配置项：`EXECUTION_STATE_DB` 可覆盖 SQLite 路径，`APP_ENVIRONMENT` 标记环境，`EXECUTION_MAINTENANCE_ENABLED` 可显式启停 FastAPI 自动维护线程。

## 11. 测试结果

- 执行状态、API jobs/projects/reviews 定向回归：70 passed。
- 项目列表状态口径修正定向回归：后端 `test_api_projects` + `test_execution_state` 共 48 passed；前端新增状态统计用例 2 passed。
- presentation/materials/insights/飞书任务调用/Agent run status/store 相关回归：208 passed。
- 前端本次相关 Vitest：29 passed。
- 前端 `npm run build` 和 `npm run lint`：通过；Vite 仅报告已有的大 chunk 警告。
- 全量前端 Vitest：90 passed、13 failed。失败集中在既有 `ProjectListPage`/`TimelinePage` 文案或选择器断言，以及 `ProjectOverviewPage` 测试没有提供 QueryClientProvider；本次新增的状态口径测试均通过。
- 全量后端 `tests/`：1500 passed、12 failed。失败均位于本次未修改的既有区域：抖音采集测试依赖不存在的 Windows MediaCrawler Python 路径 1 项；静态站点当前实现与旧 CSS/文案断言不一致 3 项；素材测试读取真实 embedding/模型配置并在受限网络下失败 5 项；orchestrator 素材补采测试的 mock `build_route_agent` 未接受当前代码已有的 `write_mode` 参数 3 项。
- 2026-08-27 本地真实接口核对：正式项目 9、已完成 6、SQLite 正在执行 0、陈旧中断 2、上次运行失败 2；6 个没有 `progress.json` 的测试/输出目录未进入列表。

## 12. 已实施和未实施

已实施：SQLite 当前状态、实例租约、backend/worker 心跳、两阶段协调、stalled、orphan interrupt、步骤收口、终态条件更新、FastAPI lifespan、pytest 隔离、观测归一化、前端状态/筛选/轮询。

未实施：自动终止 stalled 线程、跨主机高可用调度、分布式锁、自动重试策略、强制杀线程、Docker/systemd 部署文件。Python 无法安全强杀任意线程，首期只通过终态保护让迟到写入失效；重试由用户显式发起并生成新 attempt。
