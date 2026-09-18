# 开发环境搭建

## 1. 运行环境

- Python 3.11+
- 依赖管理：`requirements.txt` + venv（不引入uv/poetry，降低上手门槛）

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## 2. 核心依赖（见 `requirements.txt`）

- `langchain` / `langgraph` / `langchain-openai`：agent构建（react-agent，`langgraph.prebuilt.create_react_agent`）与主agent编排（StateGraph，见`docs/ORCHESTRATION.md`）
- `python-dotenv`：读取 `.env` 环境变量
- `pydantic`：workspace schema的数据校验
- `numpy` / `faiss-cpu`：素材库的chunk级向量检索（本地FAISS索引，不起向量DB服务）
- `rank_bm25` / `jieba`：BM25关键词检索（中文分词用jieba），跟embedding语义检索做混合检索（RRF融合），见`docs/ISSUES.md`第9条——单纯embedding对专有名词精确匹配不够可靠
- `pytest`：非LLM确定性逻辑（素材入库/检索、store权限校验）的单元测试

## 3. 环境变量（见 `.env.example`）

复制为 `.env` 并填入真实值：
- `TRAVEL_GUIDE_RUNTIME_ROOT`（可选）：统一指定 `projects/`、`data/` 和 `runtime/` 的读写根目录；不设置时仍使用仓库内原路径。它不会改变源码、Prompt 和 Agent 定义所在的仓库根，且必须在启动 Python 进程前设置；切换目录不会自动复制既有文件。
- `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL`：对话模型凭证，走`langchain-openai.ChatOpenAI`，只要是OpenAI兼容端点（包括各类relay/代理服务）都适用，不局限于Anthropic原生API
- `LLM_API_KEYS`（可选）：多个key逗号分隔（如`key1,key2,key3`），跑真实E2E经常撞到`rpm exhausted`分钟级限流导致整个进程崩溃（见`docs/ISSUES.md`），配了这个之后遇到429会自动换下一个key重试，不设置时只用`LLM_API_KEY`单key，行为完全不变。实现在`src/llm_rotation.py`
- `LLM_LIGHT_MODEL`（可选，2026-07-25接入）：轻量模型名，用于简单分类/固定字段抽取这类任务（如从一句话抽取destination/days字段、陪伴阶段问题路由到哪个agent、飞书HITL意图分类），见`src/config.py`的`get_light_chat_model`。跟`LLM_MODEL`共用同一个relay端点/`LLM_API_KEY(S)`，同样支持429轮询；不设置时默认`sensenova-6.7-flash-lite`。核心内容生成/引用真实性核验等环节继续用`LLM_MODEL`，不受这个变量影响。
- `EMBEDDING_API_KEY` / `EMBEDDING_BASE_URL` / `EMBEDDING_MODEL`：embedding凭证，**独立于上面的对话模型provider**——不是每个relay端点都提供embeddings接口（实测SenseNova这个relay就没有），所以单独配置，目前用的是硅基流动的`BAAI/bge-m3`
- `AMAP_MCP_*`：高德MCP相关配置（路线规划agent、衣食住行agent使用），未配置时`amap_query`工具走stub，不阻塞主流程
- `AMAP_GEOCODE_API_KEY` / `AMAP_JS_API_KEY`（可选，2026-07-24接入）：森林页真实地图用，见`src/tools/geocode.py`/`scripts/delivery/run_generate_site.py`、`ARCHITECTURE.md`第12.1节P2第一项。跟`AMAP_MCP_*`是不同用途、需要分别申请的key（高德开放平台同一应用下可加多个key）：前者是"Web服务API"类型，服务端调地理编码接口；后者是"Web端(JS API)"类型，前端网页里加载地图画点位用，申请时需配置referer白名单。均为个人开发者免费额度。未配置时地图区域降级为POI列表展示，不阻塞网站生成。**申请`AMAP_JS_API_KEY`时务必同时记下控制台一并显示的"安全密钥"，填入`AMAP_JS_SECURITY_CODE`**——高德2021年底起JS API强制要求先设置`window._AMapSecurityConfig`，只配key不配这个不是优雅降级，是地图加载时直接报`INVALID_USER_SCODE`报错，详见`docs/ISSUES.md`第41条。
- `FEISHU_APP_ID` / `FEISHU_APP_SECRET`（可选，2026-07-25接入，见`src/channels/feishu/`、`ARCHITECTURE.md`第14节）：飞书企业自建应用凭证，WS长连接用。`FEISHU_ENABLE_REAL_SEND`默认`false`，不真实调用飞书API（只打日志），本地开发/没有真实凭证时可以放心跑`tests/test_channels_feishu_*.py`（全部mock，不依赖这几个变量）；要真实收发飞书消息才需要配齐并显式设为`true`。
- `EXECUTION_STATE_DB` / `APP_ENVIRONMENT` / `EXECUTION_MAINTENANCE_ENABLED`（可选）：管理后台轻量级执行状态。默认使用`runtime/execution_state.sqlite3`，FastAPI lifespan 自动维护 backend 租约并协调活动任务；pytest 默认禁用自动线程并使用临时数据库。完整状态和 Docker 职责边界见`docs/EXECUTION_STATE_MANAGEMENT.md`。
- 素材源相关配置（B站/抖音/小红书，MVP阶段暂不需要，先用离线解析好的文本测试）

当前模型渠道已经不只有 `LLM_*` 一组。按 `src/config.py` / `src/llm_rotation.py` 的实际读取方式，可选独立渠道包括 `MIMO_*`、`GROK_*`、`UNISOUND_*`、`TOKENRHYTHM_*`、`CUN_*` 和 `CUNAI_*`；每组都可使用单 key 或 `*_API_KEYS` 逗号分隔的 key 池。`CUN_*` 是本地代理，`CUNAI_*` 是 cun.ai 直连，两者不能混用。当前 agentic 定向复核默认使用 `CUNAI_*`，未配置时不会自动借用旧版设计脚本使用的 `CUN_AI_*`。

其他按功能启用的配置：

- `TAVILY_API_KEY(S)`：实时 agent 和定向复核的联网补证；未配置时搜索工具返回明确的 stub 结果。
- `SILICONFLOW_ASR_*` / `MIMO_*`：素材采集转写的主渠道与兜底渠道。
- `POSTER_IMAGE_*`：`python -m scripts.delivery.run_generate_poster` 手动生成概览海报；未配置会直接报错，不影响主生成链。

## 4. 运行方式

命令行/脚本方式驱动主agent跑通一次完整流程（仍然可用，飞书接入不淘汰这条路径）：
1. 主agent状态机是否能正确流转
2. 依赖图驱动的增量更新逻辑
3. 冲突打回机制（含2次上限）
4. 素材库检索时现算可信度信号是否正确透传

飞书接入v1第一期已实现（WebSocket长连接+真正的多轮需求收集，见`ARCHITECTURE.md`第14节），
见本文档第9节的启动方式；这次范围只做WS长连接，不接Webhook，也不支持"一个群发起多次旅程"。

常用脚本：
- `python run_mvp.py`：跑一次完整的规划生成流程
- `python run_scenario_eval.py <场景id>` / `--all` / `--list`：系统性评测集（`docs/ARCHITECTURE.md`第17节），以真实用户视角跑通完整链路（生成→html→海报→陪伴阶段）验证可用性，`run_eval.py`已废弃删除
- `python -m scripts.delivery.run_annotate <project_id> <topic>`：生成带原文chunk依据的溯源版攻略
- `python run_review_mvp.py --help`：查看定向复核 CLI；支持 finding、patch、回滚、历史回填和优化候选，不会自动执行生成图级联

## 5. 测试素材

MVP测试优先使用一份**已经解析好的B站视频转写文本**（放入 `data/material_library/`），不做实时爬取/多模态解析，控制开发阶段的时延和成本。后续再考虑接入抖音/小红书及实时解析流程。

## 6. Windows开发注意事项

- faiss-cpu在Windows下不支持含中文字符的路径（本项目目录和`data/material_library/raw|index/<目的地>/`都是中文路径），`src/materials/vector_store.py`已经用"临时ASCII目录中转"的方式绕开，正常使用不需要关心这个细节，仅在改动`vector_store.py`时需要留意别绕开这层。
- 控制台默认GBK编码，运行脚本时如果打印中文报`UnicodeEncodeError`，加`PYTHONIOENCODING=utf-8`环境变量即可，不影响文件本身的UTF-8编码。

## 7. 跨机器/跨系统开发注意事项

本项目此前的开发/测试主要在Windows机器上进行，`.venv`目录（如果存在）可能是在某台特定机器上建的（含绝对路径，比如`pyvenv.cfg`里指向`E:\python-environment`这类），**不能跨机器/跨系统复用**——在另一台机器（尤其是macOS/Linux）上看到本地`.venv`时，先检查`.venv/pyvenv.cfg`确认是不是本机可用的环境，不要假设它能直接用。

如果当前机器系统自带Python版本太旧（本项目`requirements.txt`要求`langchain>=1.0`需要Python 3.10+），且没有brew/pyenv等版本管理工具，优先检查本机是否已装[`uv`](https://github.com/astral-sh/uv)（`command -v uv`）——`uv`可以不依赖brew/pyenv直接下载安装任意Python版本并建虚拟环境，2026-07-24在一台只有系统Python 3.9.6、无brew/pyenv的macOS机器上验证过可行：

```bash
uv venv --python 3.12 .venv_macos_verify
uv pip install --python .venv_macos_verify/bin/python -r requirements.txt
.venv_macos_verify/bin/python -m pytest tests/ -q
```

遇到"这台机器没法跑测试"的情况，不要直接退化成纯人工代码审查（那样的验证强度明显弱于本项目一贯的"至少mock测试真实跑过一次"标准）——先确认是否真的没有可行的Python环境搭建方式。

**B站采集pipeline（`src/materials/collectors/bilibili.py`）额外需要系统级`ffmpeg`/`ffprobe`**：`sandbox/bilibili_to_mp3/download_mp3.py`用`yt-dlp`的`FFmpegExtractAudio` postprocessor把下载到的音轨转成mp3，这一步硬依赖系统PATH里能找到`ffmpeg`/`ffprobe`二进制，`requirements.txt`里的`yt-dlp`Python包本身不包含这两个二进制。缺失时的失败模式很隐蔽：`yt-dlp`会先把音轨成功下载成`.m4a`（`sandbox/bilibili_to_mp3/output/`下能看到文件），只在最后转码这一步抛`PostProcessingError: ffprobe and ffmpeg not found`；而`collect_one`（`bilibili.py`）此前对这个失败的处理是静默`return None`（只打一行stdout `[失败]`，不留任何可事后审计的记录），非常容易被误诊断成"候选内容不相关"或"素材库该目的地真的搜不到"（真实发生过，见`docs/ISSUES.md`第77条对第75条的纠正）。

**现状（已修复，2026-07-29）**：`src/orchestrator.py`的`_attempt_auto_material_supplement`在真正开始搜索/下载前，会先调用`check_collector_dependencies()`（`bilibili.py`）做严格前置校验——ffmpeg/ffprobe缺失、或`.env`里`MIMO_API_KEY`未配置，都会在最开始就被拦下并显式抛`CollectorDependencyError`，写入`workspace/material_gap_supplement.json`的`blocked`列表+changelog（供事后一眼定位根因，不用重新调试一遍），不会再悄悄退化成"这个topic真的搜不到内容"这类误导性结果。这个校验还会自动尝试用`static-ffmpeg`这个pip包（已加入`requirements.txt`）自愈——`static-ffmpeg`自带跨平台预编译的ffmpeg/ffprobe静态二进制，`pip install -r requirements.txt`装好后`check_collector_dependencies()`检测到系统PATH里没有ffmpeg时会自动调用它的`add_paths()`把内置二进制加进当前进程的PATH，大多数情况下不需要用户手动装Homebrew。只有连`static-ffmpeg`也没装/自愈失败时才会真正报错，此时按报错提示手动装：macOS用Homebrew（`brew install ffmpeg`），或从ffmpeg.org下载静态构建后加入系统PATH。

调用`src/materials/collectors/bilibili.py`里其它函数（`download_mp3`/`collect_one`等）前如果想手动预检查，也可以直接调用`check_collector_dependencies()`，不用等到真正下载失败才发现。

## 8. 管理后台（`src/api/` + `frontend/`）本地运行

2026-07-24新增，见`ARCHITECTURE.md`第13.2节。这是独立于主链路的呈现层，不影响`run_mvp.py`等既有脚本。

**后端**（FastAPI；项目读取、运行配置、彻底重跑和定向复核写操作都通过各自 router 进入服务层）：

```bash
pip install -r requirements.txt   # 已包含fastapi/uvicorn/httpx
uvicorn src.api.app:app --reload --port 28000
```

**前端**（React + Vite + TypeScript + Tailwind CSS + React Router + TanStack Query）：

```bash
cd frontend
npm install
npm run dev      # 默认18888端口，Vite dev server代理/api到localhost:28000，不需要单独配CORS
npm run test     # Vitest单元测试
npm run build    # 生产构建（含tsc类型检查）
```

前端默认代理到后端 `28000` 端口（`frontend/vite.config.ts` 的 `server.proxy`）。如果该端口被占用，需要同时修改代理目标和 `uvicorn --port` 参数。

## 9. 飞书WS长连接worker（`run_feishu_worker.py`）本地运行

2026-07-25新增，见`ARCHITECTURE.md`第14节。这是独立于主链路/管理后台的第三个入口，跟`run_mvp.py`/`uvicorn src.api.app:app`都不冲突，可以同时跑。

**前置条件**：`.env`里配置`FEISHU_APP_ID`/`FEISHU_APP_SECRET`（飞书开放平台"企业自建应用"的凭证）；本地开发不想真实发消息到飞书群时，保持`FEISHU_ENABLE_REAL_SEND=false`（默认值）即可，`ws_client.py`会把要发的消息打印到控制台而不是真实调用飞书API。

```bash
pip install -r requirements.txt   # 已包含lark-oapi
PYTHONIOENCODING=utf-8 python run_feishu_worker.py
```

**为什么是独立进程**：WS长连接的`.start()`是阻塞调用，会一直占住线程/进程，不能跟uvicorn等其他Web服务塞进同一个多worker/热重载进程里（否则同一个`app_id`会重复建立多条长连接，飞书侧报冲突或消息重复投递）。`run_feishu_worker.py`内置文件锁（`data/feishu_ws_worker.lock`）防止手滑重复启动多个实例——Windows用`msvcrt.locking`、POSIX用`fcntl.flock`，拿不到锁直接以exit code 3退出，不做等待重试。

**已知局限（如实标注，这次范围内没有做）**：只支持WebSocket长连接，不支持Webhook；一个飞书群固定绑定一个project（`src/channels/feishu/bindings.py`），不支持同一个群切换/发起第二次旅程；没有真实飞书环境下的联调验证，只做到mock测试层面（`tests/test_channels_feishu_*.py`），真实WS收发消息需要用户提供真实凭证后另行验证。

## 10. 定向复核本地验证

定向复核有两类入口：`run_review_mvp.py` 提供确定性 finding / patch 操作，管理后台 `/api/projects/{project_id}/review-*` 路由还提供模型建议、逐条人工决定、影响预览和跨项目队列。复核 agent 只能读取目标内容、素材和历史记录，不能直接写攻略文件；正文修改必须经过 patch propose / apply。

```bash
python run_review_mvp.py findings --project-id <project_id>
python run_review_mvp.py optimization-candidates --project-id <project_id>
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/test_review_loop.py tests/test_review_agent.py tests/test_api_reviews.py -q
```

不配置真实模型 key 仍可运行确定性 store / API 的 mock 测试；真实生成 recommendation 时，默认渠道需要 `CUNAI_API_KEY` 或 `CUNAI_API_KEYS`。测试通过只证明当前测试覆盖的逻辑，不代表真实 provider、联网搜索或历史项目数据已经完成端到端验收。
