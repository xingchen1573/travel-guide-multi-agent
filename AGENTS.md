# Codex 协作入口

- 根据 `docs/README.md` 按任务选择相关文档，不默认加载全部历史、问题或设计记录。
- 修改 Agent 职责、Prompt、工具权限、输入输出或契约时，读取 `docs/AGENTS.md`；Prompt 结构约定另见 `docs/PROMPT_CONVENTIONS.md`。
- 调试、根因分析或判断模型结构性局限时，读取 `docs/DEBUGGING_RULES.md`，并按关键词检索 `docs/ISSUES.md`。
- 进行任何 Git 写操作前，读取 `docs/LOCAL_GIT_WORKFLOW.md`。
- 没有用户对本次操作明确说出“提交”或 `commit`，不得执行 `git add`、`git commit` 或其他修改 Git 索引和历史的命令。
- `git push`、远端配置和历史改写必须分别获得当次明确授权。
- 个人顺序开发不强制使用额外 worktree；两个及以上会写入文件的任务并行运行时，必须使用独立分支和独立 worktree。写入任务与依赖稳定代码快照的并行验证也必须隔离；纯只读任务可共享 checkout。细则见 `docs/LOCAL_GIT_WORKFLOW.md`。
- 保留用户及其他任务的并发修改，不得擅自撤销、覆盖、暂存、清理或纳入提交。
- 代码和可复现证据优先于可能漂移的文档；结论必须区分当前实现、设计、提案和历史。
- 业务事实保持单一权威来源；新建或调整文档时遵循 `docs/DOCUMENTATION_STANDARD.md` 并登记到 `docs/README.md`。
- 启动、接管或恢复可能持续较久的任务时，必须读取并遵循 `.agents/skills/long-running-task-supervisor/SKILL.md`。只要受监督的进程、任务或既定恢复动作尚未进入可证终态，就只能发送进度更新并继续等待、检查或恢复，不得发送最终答复后离开；等待工具暂时无输出、墙钟较长或状态文件暂未更新都不是终态，也不得据此发送 `Ctrl-C`、terminate 或其他停止信号。
