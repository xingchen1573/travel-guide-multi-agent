# tests/

测试集覆盖 Workspace 权限与 Schema、素材采集和混合检索、LangGraph 路由、冲突打回、断点续跑、飞书渠道、管理后台 API、静态网站/海报、模型轮换，以及定向复核与 Patch 闭环。

## 运行方式

```bash
# 全量后端测试；默认不调用真实业务 LLM
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/ -v

# 只收集用例，不执行
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/ --collect-only -q -p no:cacheprovider

# 常用的定向测试
python -m pytest tests/test_orchestrator_graph_level.py -v
python -m pytest tests/test_materials.py tests/test_retrieval.py -v
python -m pytest tests/test_review_loop.py tests/test_review_agent.py tests/test_api_reviews.py -v

# 前端测试
cd frontend && npm test
```

截至 2026-08-26，限定 `tests/` 目录的只读收集结果为 **1423 tests collected**。该数字是用例数量快照，不表示当前版本已经有 1423 个测试通过；通过数必须以一次明确记录了命令、环境和失败项的实际运行结果为准。若从仓库根目录无范围执行 pytest，还会收集 `sandbox/` 内第三方项目测试，并因其额外依赖未安装而在收集阶段报错，因此不使用该口径代表本项目测试集。

## 覆盖边界

- Agent 工具调用和 Prompt 边界主要使用 Mock 模型或固定 Fixture 验证，避免测试消耗真实额度或受供应商波动影响。
- `build_graph()`、生产入口、Send fan-out、interrupt/resume 和编辑级联已有图级或入口级测试。
- 定向复核测试覆盖 Finding、claim 证据账本、逐条人工决定、Patch 版本校验、应用、拒绝、冲突、回滚、状态扫描和优化候选。
- `run_scenario_eval.py` 属于真实系统评测，不等同于单元测试；它可能调用真实 LLM、Web、高德和生图服务，耗时和结果受外部环境影响。
- 依赖本机素材状态、第三方采集子进程或前端 CSS 字面值的用例可能暴露环境型失败。记录测试结果时必须把这些失败逐项列出，不能只写一个笼统 passed 数字。

新增共享行为、跨模块契约或用户可见工作流时，应同时补充对应的确定性测试；真实 E2E 只用于验证完整链路和内容质量，不能替代回归测试。
