# 旅游攻略多Agent助理架构

> 本文档维护当前行为层架构、模块边界和稳定设计原则，不再累积完整实施时间线。历史设计和验证按需进入已登记的设计、问题或复盘文档；文末只保留旧版本索引。第4/5/6节（状态机、依赖图、打回机制）的具体技术实现（LangGraph图结构、依赖表、路由函数）见 [`ORCHESTRATION.md`](ORCHESTRATION.md)，本文档保留行为层面的设计意图，实现细节以代码、测试和专题权威文档为准。
>
> 生成过程的日志审计、证据血缘、流程观测和跨项目系统评测设计见 [`docs/AUDIT_OBSERVABILITY_AND_EVALUATION_DESIGN.md`](AUDIT_OBSERVABILITY_AND_EVALUATION_DESIGN.md)。该专题设计当前已定案但尚未实现，与现有生成后兜底机制职责分离。

---

## 1. 项目定位

用户通过CLI或飞书与主Agent交互，描述一次旅游需求（如“三天成都旅游”）。系统通过多Agent协作完成需求收集、素材调研、路线规划、内容核验、攻略整合和重点POI详情生成；每次旅行是一个独立Project，结构化事实和产物保存在自己的Workspace中。

当前系统还包含飞书陪伴与提醒、静态网站和海报、管理后台，以及生成后的Finding、人工裁决、Patch和定向验收维护链路。当前重点是通过真实场景和可复现证据持续提高引用、时效性、路线可执行性和长流程恢复质量，而不是继续以“跑通MVP”描述项目阶段。

---

## 2. 核心设计原则（不可违反的几条约束）

1. **主agent是唯一调度中心**：子agent之间不直接协作、不直接依赖、不直接调用彼此。所有的触发、联动、打回都由主agent决策和发起。
2. **每个agent是独立的react-agent**，有独立工作空间（独立工作目录）、独立人设，同时也支持被外部单独调用/执行（用户可以单独@某个agent）。
3. **写入权限分层**：
   - 各agent可以直接写自己的"产出区"。
   - **"最终攻略"（合并稿）只能由整合agent写**，其他任何agent都不能直接改最终稿，保证最终稿始终是唯一、去冲突后的版本。
   - 实时agent对公共区的修改需经主agent审批，且必须留痕（变更日志）。
4. **素材库只增不改（append-only）**：核心素材agent入库时只做轻量的单条元数据提取，不做跨条目比对/不回改旧记录，避免维护成本随库增大而失控。可信度/一致性判断放到检索时现算，而不是入库时固化。
5. **修改和联动都走依赖图**，不是靠agent临时判断或读聊天记录猜测。

### 2.1 Codex 与 LangGraph 的共享 Kernel 边界

`travel_guide_kernel` 是两个运行入口共同消费的 Python API 边界。`travel_guide_kernel.route` 是路线结构快照、哈希、差异和确定性校验的唯一实现；`travel_guide_kernel.models` 是路线规划、实体解析、执行计划和可行性报告数据契约的唯一定义；`travel_guide_kernel.schema`、`store` 和最终攻略纯函数共同拥有 Workspace 数据契约与读写实现。旧 `src.workspace.schema/store/final_guide_*` 只保留兼容导入，Codex bridge/preflight、LangGraph orchestrator 与固定路线初始化器仍使用同一个 Store 模块对象。`projects` 暂时仍是固定路线初始化器的宿主兼容门面，旧 `src.workspace.route_validation` 也只保留兼容导入。

仓库根 `pyproject.toml` 可以把这部分边界构建为 `travel-guide-kernel` wheel，且只收录 `travel_guide_kernel/`。安装后的路线、Schema 和 Workspace Store 不依赖宿主源码；配置 `TRAVEL_GUIDE_RUNTIME_ROOT` 后可以独立创建、读写 Project。`projects` 模块可被安全导入，但固定路线初始化操作仍需要主项目 `src`。

当前 Store 的跨进程文件锁使用 POSIX `fcntl`，首版可移植目标为 macOS/Linux；Windows 尚未完成运行验证。

因此这还不是可独立运行的完整旅游系统：`projects/`、`data/` 和 `runtime/` 的读写根已可通过 `TRAVEL_GUIDE_RUNTIME_ROOT` 统一指向源码仓库之外，但 Provider、素材检索、live 路线核验、Prompt 和运行编排仍属于现有宿主。后续只有在真实安装验证要求这些模块继续解耦时才迁移；在此之前不复制第二套业务实现。

---

## 3. Agent 清单与职责

### 3.1 核心生成与详情Agent

| Agent | 职责 | 依赖 |
|---|---|---|
| **主agent** | 与用户交互收集需求；两阶段确认（需求确认→骨架确认）；维护项目状态机；调度所有子agent；处理依赖联动、打回决策、写入审批 | 无 |
| **核心素材agent** | 从B站/抖音获取并维护素材库；只做单条元数据提取入库，定期补充。维护的是**按目的地分区、跨project共享的库**（不是按project_id分区）——检索靠"目的地"这条轴隔离，不同project只要目的地相同就共用同一份处理好的素材，避免重复抓取处理；可被主agent在规划开始前作为前置校验触发（目的地没素材就先补），也可被用户随时单独@做维护性调用/**自主采集**（根据需求自主搜索B站/抖音候选，人工确认后才真正下载入库，见`src/agents/material_agent/collection_graph.py`），触发都不影响当前project的状态机/依赖级联，见`docs/ORCHESTRATION.md`第3.1/3.1.1节 | 无 |
| **路线规划agent** | 产出几天几夜的大框架行程（骨架），作为下游基准；结合高德MCP做结构化校验（距离、开放时间、POI真实性）；对自驾/长线穿越类目的地，用`web_search`核实途经关键道路当前是否正常通行、有没有长期性封闭/限流/资质变更（2026-08-01新增，见`docs/ISSUES.md`第93条） | 核心素材agent（素材）+ 高德MCP + `web_search` |
| **衣食住行agent**（合并美食/穿搭+住宿/游玩） | 结合路线规划产出，综合调研每天的美食、穿搭、住宿、游玩建议；内部并行调用检索工具（素材库/天气/POI等），但由**同一次LLM推理**完成最终建议的综合，保证美食/穿搭/住宿/游玩之间天然一致、不自相矛盾；产出区内部仍按四个子领域分字段存储，便于后续按需局部修改 | 路线规划agent |
| **整合agent** | 汇总以上所有产出，去冲突、整理，**独占最终攻略的写入权**；同时负责"必备清单"这一独立子任务 | 衣食住行agent产出 |
| **POI详情agent** | 针对路线规划agent标记为`needs_detail=true`的"重点景点"（本身构成一次独立、有明确路线/途经点概念的游玩体验，如山岳景区/古镇/大型园林，区别于奶茶店这类普通打卡点），产出深度玩法路线（途经点顺序、每点亮点、建议停留时间），**不覆盖吃/穿/住**——这些仍由衣食住行agent负责，两者是"概览vs细节"关系不重叠。**整合agent产出最终攻略后自动触发**（不是用户按需请求），对多个重点景点**并行**生成（详见`docs/ORCHESTRATION.md`），全部完成后统一反向同步回最终攻略正文 | 整合agent产出（最终攻略确定后才知道有哪些重点景点）；反向触发整合agent二次合并 |

> **森林与树木的分层设计**（本节新增，见下方3.4节详述）：`route_agent`→`lifestyle_agent`→`integration_agent`这条主链路产出的是**森林**（整体规划，一眼概览）；`POI详情agent`产出的是**树木**（单个重点景点的深度玩法路线），是独立的新产物类型，不是对森林正文的编辑。两者通过文件级引用关联，森林正文里对应POI处会有一段程序拼接的摘要+跳转链接指向树木文件，供未来网站分层展示、生图API取用途经点级别的图片提示词。

### 3.2 陪伴与扩展Agent（已实现）

随身向导Agent、实时Agent、避雷Agent和文化科普Agent均已实现，不属于未来v2范围。它们的当前职责、工具和写权限由 [`AGENTS.md`](AGENTS.md) 维护；陪伴阶段路由与审批由 [`ORCHESTRATION.md`](ORCHESTRATION.md) 维护。未来新增Agent应先进入独立设计并明确成熟度，不再沿用“v2延后”作为笼统状态。

### 3.3 衣食住行agent的合并考量（折中方案）

最初设计是美食/穿搭+住宿/游玩三个独立agent，各自并行执行、互不通气，但这带来两个问题：一是三者之间容易产生跨agent冲突（如游玩排了爬山、穿搭却不知道），需要靠整合agent+打回机制事后补救；二是MVP阶段要维护三套独立agent、三条依赖边，复杂度偏高。

权衡后采用折中方案——**合并为一个agent身份，但内部保留并行**：
- 对外只有一个"衣食住行agent"，用户可以整体@它，也可以在消息里指明只想改某个子领域（如"美食"），由该agent自己识别并只更新对应字段。
- 对内，检索素材/调用天气或POI等工具时可以并行发起（多个工具调用同时执行），但**最终把这些信息综合成建议是同一次LLM推理完成**，天然保证美食/穿搭/住宿/游玩之间不会互相矛盾，省去了这层跨agent冲突处理的成本。
- 产出区按四个子领域分字段存储（而非一整块文本），为后续局部修改和必要时拆回独立agent留出空间。

代价：损失的是"顶层任务级别"的并行（原本3个agent级别的并行，变成1个agent内部的工具级并行），单次调用的上下文/token量也会更大。如果后续发现耗时或prompt复杂度成为瓶颈，可以按子领域拆回独立agent，因为内部逻辑本来就是模块化存储的，拆分成本不高。

### 3.4 POI详情agent（森林与树木分层设计）

**背景**：现有主链路只能产出"几天几夜"粒度的攻略（如"Day2去青城山"），用户真正执行时缺少可操作的细节（怎么爬、途经哪些点、每个点做什么）。为此新增一层**树木**（POI详情），跟原有主链路产出的**森林**（整体规划）形成"整体+局部"的文件组结构，为后续网站分层展示、生图API调用做准备。

**关键设计决策**：

1. **树木是独立新产物，不是编辑现有内容**——独立文件（`workspace/outputs/poi_details/`，见`docs/WORKSPACE.md`），不走edit模式那套"修改已有字段"的语义，因为它要给网站/生图API用，需要结构化字段（途经点坐标级信息、image_prompt等），塞进现有偏自然语言正文的字段里不合适。
2. **只覆盖"怎么玩"，不覆盖吃/穿/住**——跟`lifestyle_agent`是"概览vs细节"关系，不是替代关系：`lifestyle_agent`的`play`字段管"要不要去、大致体验"，POI详情agent管"具体怎么走"。这样改动范围可控，不需要推翻已验证过完整E2E的`lifestyle_agent`。
3. **自动触发，只覆盖"重点景点"**——整合agent产出最终攻略后自动触发（不等用户请求），但不是对行程里每一个POI都生成，只对`route_agent`骨架阶段标记`needs_detail=true`的点（本身构成独立游玩体验、有明确路线概念的景区，区别于奶茶店这类打卡点）生成，控制自动化成本。判断由`route_agent`在生成骨架的同一次推理里顺带完成，不新增调用成本。
4. **真并行生成**——多个重点景点用LangGraph `Send` API做fan-out并行调用，不是顺序循环。这是本项目第一次引入跨agent级别的并行（此前的"并行"只存在于`lifestyle_agent`内部的工具调用级别），生产级系统不能接受多个景点顺序生成的时延，限流风险通过多API key轮询解决（见`src/llm_rotation.py`），不作为放弃并行的理由。
5. **森林反向同步**——所有并行分支**全部完成后**（join，不是谁先完成谁触发），统一触发一次整合agent二次合并，把树木内容体现进森林正文：具体做法是**纯程序拼接**（不重新调用LLM）——从每个树木文件里抽取`recommended=true`的那条路线的途经点摘要，追加到森林正文对应POI处+跳转链接。这样不会给已核验过的森林正文重新引入幻觉风险，也不需要重新走一遍`citation_check`。
6. **多路线支持**——一个POI可能有多条实际存在的路线（如青城山前山/后山），schema用`routes`数组承载，每条路线标注`recommended`+`recommend_reason`（依据已收集的`requirements.json`用户偏好判断，不新增"问用户需求"的步骤）。（**2026-07-26更新**：生成时的自动判断只是初始值，用户事后可以通过edit入口（第9点）要求切换推荐路线/补充新路线，"多骨架备选"从"模型自己选一次"推进到"用户可以自己选"，见`docs/ISSUES.md`相关记录）
7. **素材不足时的处理**——Route、Lifestyle和POI详情核验在确认真实素材缺口后，可以进入共享的限量自动补充机制；Pitfall当前不接入。Project累计最多自动补充10条、单次事件最多2条，新素材只有完成入库并产生真实entry和chunk后才获得一次局部修复与复核机会。预算耗尽、采集失败或仍无足够证据时必须如实保留缺口，不得编造补全。完整边界见第16节。
8. **核验与失败处理**——复用现有`citation_check`机制核验途经点的事实性描述（`image_prompt`这类衍生创作内容不需要逐字核验）；打回`MAX_RETRY`次仍不过时走现成的`mark_force_finalize`，该POI详情如实标注"存在未核验通过的内容"，不阻塞其他POI和整体流程进入"完成"状态。
9. ~~已知局限（这次不做）：POI详情agent暂不接入edit模式入口（不支持用户事后单独调整某个途经点描述），跟现有`route_draft_node`同样未接edit入口的局限一致——这套模式已经在`lifestyle_agent`/`integration_agent`上验证可用，以后要加是照搬现成模式，不是从零设计。~~ **已实现（2026-07-26）**：新增`poi_detail_edit_node`（`entry_target="poi_detail_edit_node"`，粒度为"路线"——对称`route_agent`的"天"、`lifestyle_agent`的"字段"），支持三种意图（由模型自然语言判断，不做硬编码字符串匹配）：①修改某条已有路线的具体内容；②切换推荐哪条路线（多骨架备选，呼应第6点）；③新增一条此前不存在的路线；还支持"首次生成"（该POI当初没有被标记`needs_detail=true`，用户事后单独要求补充生成）。跟`route_draft_node`/`lifestyle_agent_node`同款误改检测（一次edit通常只涉及一条路线，改动超过1条`report_conflict`留痕），核验复用`_run_poi_detail_citation_loop`（跟`poi_detail_agent_node`共用同一份实现）+ 核验范围收窄到真正改动/新增的路线（`verify_poi_detail_citations`新增`route_names`参数，同`fields`/`item_keys`思路）；**纯粹切换推荐路线不触发citation_check**（不涉及新的事实性陈述，沿用原有`citation_status`，避免非确定性判断误伤）。这是"一跳结束"的edit入口（不级联`citation_check_node`/`pitfall_agent`/`integration_agent`——POI详情本来就不在`AGENT_DEPENDENCY_TABLE`级联链路里），`_route_edit_mode`新增`last_agent=="poi_detail_agent"`直接`END`分支，跟`culture_agent`同款模式。反向同步`manifest.json`/`final_guide.md`时会重建"全部已生成POI"的完整列表（从manifest读取），不会冲掉其它未被这次编辑触及的POI。`store.write_poi_detail`新增`_clamp_single_recommended_route`确定性兜底（多条`recommended=true`时只保留第一条，不依赖prompt自觉遵守）。**2026-08-06改为双工具并存**：`write_agent_output`整份覆盖用于首次生成/新增/删除路线，`write_agent_output_patch`用于改内容/切换推荐。**2026-09-02继续收窄自动返修粒度**：不是新增段级工具，而是让原Patch按`route_name + route_segments[].order`分层合并。引用失败时每个途经点成为独立工作单元，只携带该点完整结构、只复核该点；未提交的相邻途经点、路线定位和人工照片由Store保留。路线级编辑仍可一次提交多个途经点，结构性删除/重排仍必须整份覆盖。详见`docs/ISSUES.md`第113、163条。越权检测阈值（>1条路线视为可疑）跟其它agent同款，可能有同样的边界case未穷尽，是已知局限。

对应的LangGraph图结构/并行fan-out/依赖表/状态机技术实现见`docs/ORCHESTRATION.md`；文件schema见`docs/WORKSPACE.md`。

### 3.5 陪伴阶段设计：随身向导 / 实时agent / 衣食住行agent / 避雷agent的协作路由（2026-07-20设计讨论；随身向导agent/实时agent查询/意图路由已实现，见v1.13变更记录，2026-07-21；避雷agent`pitfall_agent`本身已实现（2026-07-23），陪伴阶段"再帮我查查这家店有没有坑"复用edit模式入口的路径也已随实现同步验证，见本文档变更记录）

**背景**：森林+树木生成完、project进入"完成"状态之后，用户还会持续跟系统对话（"今天去哪""这个点附近吃什么""现在天气怎么样""这家店还有坑吗"），这是攻略生成完之后的**陪伴**能力，不是"完成"这个静态终态所能覆盖的。

**核心原则不变**：`主agent是唯一调度中心，子agent之间不直接协作、不直接调用彼此`（第2节第1条硬约束）——陪伴阶段涉及多个角色分工，但角色之间不互相调用，都是主agent做判断、发起调用。

**角色分工**：

1. **随身向导agent**（新增，第3.2节）：只读已生成的森林/树木数据，回答"读取已有攻略"这类问题，不联网、不检索素材库、不写公共区。是这套设计里最轻量、零风险的一环。
2. **实时agent**（既有设计，第3.2节，定位收窄）：只处理"真正需要联网才能回答"的时效性问题（天气/临时闭馆/最新事件），写入公共区仍必须走现有的`await_realtime_approval`审批流程，这条线不因为陪伴阶段的交互更频繁就放松。
3. **衣食住行agent**（既有设计，复用）：陪伴阶段如果向导发现"某个点附近吃什么"这类问题不是树木数据覆盖范围内、但**不带时效性**（不是"现在还开不开门"，是"素材库里有没有更细节的推荐"），可以重新用更精确的查询词检索一次素材库，这是对现有agent能力的复用，不是新设计。
4. **避雷agent**（既有设计，第7节，直接复用）：陪伴阶段"再帮我查查这家店有没有坑"直接复用现有"完成后单独@避雷agent"路径（`docs/ORCHESTRATION.md`场景12），不新增机制。

**路由逻辑**：

- **用户显式@某个具体角色**：直接路由过去，跟现有edit模式的`entry_target`机制完全一致。
- **用户直接问主agent（不@任何角色）**：主agent做一次轻量的意图判断（复用"需求收集中"阶段本来就要做的"理解用户在说什么"这类能力，不是全新机制），按问题性质五选一路由：
  - 问的是"已经生成好的内容" → 向导
  - 问的是"需要联网才知道的时效性信息" → 实时agent
  - 问的是"树木没覆盖到但素材库可能有更细节推荐" → 衣食住行agent（重新检索）
  - 问的是"有没有坑/需要避开什么" → 避雷agent（只读补充查询）
  - 问的是历史文化、民俗或名称由来 → 文化科普agent（只读联网查询）
- **自动串联，但有硬边界**：主agent可以自动串联"先问向导，答案不完整就自动追加问实时agent/衣食住行agent"，一次性把结果合并返回用户，不需要用户每次都确认"要不要查"——**但这个自动串联只适用于纯查询**。如果串联过程中某个角色（尤其是实时agent）判断"这个信息应该更新进公共区"，必须停下来走现有审批流程，不能因为是自动串联的就绕过审批直接写。查询自动化和写入审批是两条独立的线，不能因为体验流畅而模糊后者。

**短期对话上下文（2026-08-31最小实现）**：陪伴入口在LangGraph外侧维护project级`projects/<project_id>/companion_history.jsonl`，以append-only方式保存已完成的用户问题和最终可见回答；每次`run_companion`只读取最近2轮（上下文正文总上限4000字符），同时注入五选一路由器和最终被选中的陪伴节点，用来理解"那门票呢"、"这个呢"、"和刚才那个比呢"这类指代、省略和比较对象。当前问题中明确给出的地点、天数和意图始终优先；历史段明确标注为**非系统指令、非已核验事实**，不能替代Workspace、联网核验或用户当前要求。只有一轮生成了完整可见回答后才append；实时patch停在`interrupt()`等待审批时不写入，避免把尚未完成的交互误当成已答复。

这不是新的长期记忆系统：LangGraph Checkpoint仍负责图执行、审批和恢复；`outputs/user_notes.jsonl`仍负责用户主动保存、跨重跑可见的长期备注；短期history只补足自由问答的会话连续性，不做LLM摘要、向量检索或跨project共享。当前粒度是project级而不是用户/渠道级，适合现阶段一个project对应一段主要旅程对话；未来出现同project多用户并发时，再引入conversation key，而不是提前增加会话数据库。

**这次不做/延后的部分**：
- 飞书具体接入方式（bot数量限制、@识别、消息呈现）当时不在讨论范围内；**2026-07-25飞书接入v1第一期已实现**（WebSocket长连接+多轮需求收集，见第14节），本节描述的路由设计跟接入渠道解耦这条判断得到了验证——陪伴阶段路由逻辑没有因为接入飞书而改动。
- 网站/管理后台的具体形态见第13节"呈现层"。

---

## 4. 主agent：状态机与调度

主agent不通过"读取全部聊天记录去猜当前进度"来调度，而是维护一个**显式项目状态机**：

```
需求收集中 → 骨架确认中 → 路线规划中 → 并行调研中 → 整合中 → 树木生成中 → 完成
                                              ↑________|
                                          （打回重跑，见第6节）
```

- **需求收集中**：与用户多轮交互，确认天数、类型风格、偏好、注意事项、预算等。
- **骨架确认中**：路线规划agent产出初版大框架后，向用户展示，等待用户确认/调整。**骨架确认前的修改不会影响下游**，因为下游agent此时还未开始调研。
- **路线规划中**：路线规划agent结合素材+高德MCP产出确定版行程。
- **并行调研中**：衣食住行agent执行，内部并行调用检索工具，一次综合推理产出美食/穿搭/住宿/游玩四个字段，写入自己的产出区。
- **整合中**：整合agent汇总去冲突，形成最终攻略（森林）；必备清单作为其子任务一并产出。
- **树木生成中**（3.4节新增）：整合完成后**自动**触发，对`route_plan.json`里`needs_detail=true`的重点景点并行调用POI详情agent生成深度玩法路线（树木），全部完成（join）后统一触发整合agent二次合并，把树木摘要反向同步进森林正文。这一阶段是主动自动化流程，不是"完成"状态那种被动响应模式，因此单独建模为显式阶段，供进度展示/监控用。
- **完成**：用户可后续单独@任意agent（含v2的文化科普/实时agent）追加提问或发起修改。

骨架确认之后发生的任何修改（无论是整合agent自动发现冲突，还是用户主动@某agent），都通过**依赖图**（见第5节）判断影响范围，而不是全量重跑。

---

## 5. 依赖关系图与增量更新

### 5.1 依赖边

```
路线规划agent → 衣食住行agent
衣食住行agent → 整合agent
衣食住行agent → 避雷agent → 整合agent（避雷agent针对衣食住行agent具体推荐的内容做针对性检索，
                                  所以是真实依赖，排在衣食住行agent之后跑，不是独立并行；2026-07-23已实现）
整合agent → POI详情agent（并行，多实例）→ 整合agent（二次合并，反向同步森林，见3.4节）
```

> 原本美食/穿搭住宿/游玩三者之间没有依赖边，是因为它们各自独立执行、互不通气，这也是跨agent冲突的根源。合并为衣食住行agent后，这类"域内冲突"变成了同一个agent内部一次推理的产物，天然减少矛盾，依赖图和第6节打回机制的适用范围也相应收窄——主要用于处理路线规划本身的问题，以及衣食住行agent与避雷内容之间的冲突，不再需要处理"美食vs游玩"这类同域内部冲突。
>
> `整合agent → POI详情agent → 整合agent`这条边是一个环——整合agent先产出一版森林，森林里标记为`needs_detail=true`的POI才触发POI详情agent（且是并行fan-out多实例，不是单条边那种一对一），POI详情agent全部完成后再回到整合agent做一次轻量的二次合并（程序拼接，不重新走LLM推理正文，见3.4节第5点）。这跟"打回重新生成"的环不是一回事，不占用`MAX_RETRY`打回次数。

### 5.2 修改触发流程

1. 用户直接@某个具体agent（如衣食住行agent，可在消息中指明只想改哪个子领域，如"美食"）提出修改。
2. 该agent更新**自己产出区里对应的子领域字段**（可直接写，无需经过整合agent；只重新推理被要求的字段，不必重跑全部四个维度）。
3. 该agent通知主agent。
4. 主agent查依赖图，判断这次修改是否需要联动其他agent：
   - 例：改路线 → 按依赖图联动衣食住行agent重新调研。
   - 例：只改衣食住行agent里美食的一道菜 → 不联动路线，只需触发整合agent重新合并。
5. 主agent触发整合agent，**只重新合并受影响的部分**（不做全篇重跑），更新最终攻略，并记录变更。

> 特例：骨架规划阶段（下游还未开始调研）修改路线，不触发任何联动，因为此时依赖图上还没有下游产出需要更新。

---

## 6. 冲突处理与打回机制

- 整合agent在合并时发现有明确上游依据的跨模块冲突（如Day归属、城市/区域字段、用户硬约束或已有高德核验结果互相矛盾），**没有直接调用其他agent的权限**（遵守"子agent不互相调用"原则）。它不重新核验引用，也不凭自己的地理知识推断地点距离。
- 整合agent将冲突详情（具体是什么问题、涉及哪个agent）报告给主agent。
- 主agent决定是否、以及触发哪个agent重新调研，**并把整合agent发现的具体冲突点作为上下文传回去**（不是让agent盲目整体重做）。
- **打回次数上限：2次**。超过上限后，整合agent不再打回，而是在最终攻略中标注"存在未解决的冲突"，如实呈现给用户，避免无限循环。

---

## 7. 避雷内容的冲突处理（2026-07-23已实现，见`src/agents/pitfall_agent/`）

- 避雷agent**排在衣食住行agent之后跑**（见第5.1节依赖边），针对衣食住行agent已经产出的具体推荐（某餐厅/某住宿/某景点）做针对性检索，而不是笼统查目的地层面的注意事项——这样查到的避雷内容才是"这家店有没有坑"，而不是"成都旅游要小心什么"，更有实际参考价值。
- 其他领域agent（现为衣食住行agent）在生成建议时也可以参考素材库里已有的避雷相关内容，但避雷agent的定位是补充和兜底，不是替代衣食住行agent自己的判断。
- 避雷agent的产出最终提供给**整合agent**作为补充参考，融入到各部分中，形式是"补充预警标注"，而不是替换原有建议。
- 冲突优先级默认规则：
  1. 默认以衣食住行agent自己的内容为主（因为其上下文更完整、更聚焦）。
  2. 仅当避雷内容的**来源数量**明显超过衣食住行agent原建议的来源数量时，升级为"需要重新考虑该推荐"，交由整合agent用规则+LLM判断处理——这种情况下整合agent会调用report_conflict，target指向衣食住行agent对应的字段，触发打回重新生成（见`docs/ORCHESTRATION.md`打回机制），而不是避雷agent直接覆盖内容。
- 这里复用的"来源数量"字段，与第8节素材可信度机制是同一套信号，不需要额外建设。
- 打回重新生成衣食住行agent的内容后，避雷agent也需要针对新的推荐重新检索一遍（因为它依赖衣食住行agent的具体产出），这个级联关系在`docs/ORCHESTRATION.md`的依赖表里体现为`lifestyle_agent`的`downstream`包含`pitfall_agent`。

---

## 8. 素材库设计

### 8.1 入库（核心素材agent，per-doc轻量处理，append-only）

每条素材只做**一次性、单条**的元数据提取，不做跨条目比对：
- 来源平台（B站/抖音/小红书）
- 作者/UP主ID
- 发布时间
- 提及的实体（地名/店名/景点名等）
- 原文/转写内容片段
- **是否与目的地相关**（质量门禁，见下）

新素材直接追加进库，**不需要回头修改任何已有记录**，维护成本恒定，不随库增大而升高。

**质量门禁（is_relevant）**：入库前先判断"这条素材是否真的是关于该目的地的真实旅游/生活经验"，
不相关内容（无关地区被关键词误召回、网络热梗/影视吐槽段子剪辑、广告软文）**完全不写入
entries/chunks/FAISS索引**——不能只标`is_core=false`，因为检索不按`is_core`过滤，标false挡不住
被检索到，必须整条跳过写入。这个判断复用单条元数据提取本来就要做的那次LLM结构化输出调用
（entities抽取），加一个`is_relevant`+`reason`字段，不新增调用成本。判断结论记录在
`index/<topic>/rejected.jsonl`（source_file+理由+时间），避免下次重跑重复判断同一条，同时
留痕方便以后复核。`raw/`下的原始文件不删——质量判断属于入库层的决定，不应该反过来动人工维护的
原始输入层。

**内容长度门禁（`/grilling`访谈定案，2026-08-07，尚未实现）**：入库主循环目前只有`if not content`
这一个空值检查，没有最小长度阈值——原文只有几个字的素材（无实质信息量，几乎不可能承载可检索的
具体细节）也会照常送进`_analyze_content`走一次LLM调用、判定`is_relevant=true`后正常切chunk/
embedding入库，白白消耗LLM调用还污染索引（可能在BM25里意外命中宽泛查询、挤占真正有信息量的
结果排名）。**方案**：
- 阈值：统一20字（原文字符数），不按平台/内容类型分层——`bilibili_asr`/`douyin_asr`是几百字起的
  口播转写全文、`douyin`平台内容采集时已过`_THIN_CONTENT_THRESHOLD=60`的自动补ASR判断、
  `xiaohongshu`是多模态图片描述+可选补充文字合成，正常情况下都不会命中一个这么低的阈值——
  20字只用来挡"手动方式随手丢入的极短测试内容"这类明显没有意义的边缘情况，不追求精确卡出
  "内容是否语义充实"的分界线。
- 检查时机：在`_analyze_content`（LLM调用）**之前**、`if not content: continue`**之后**——
  空值检查最便宜排最前，长度检查次之（纯规则、零成本），真正花钱的LLM相关性判断放最后，
  层层递进省成本。
- 记录方式：跟`is_relevant=false`的拒绝**写入同一个`rejected.jsonl`**，不新开文件——两者本质上
  是同一类事件（"这条素材没有真正进入索引"），审计时需要合并查看"这次入库总共拒绝了哪些、
  分别因为什么原因"。`reason`字段用文字区分因由（如`"内容过短（N字，低于20字阈值），入库前跳过"`），
  不额外加拒绝类型字段。

**entities质量改进（`/grilling`访谈定案，2026-08-07，已实现，2026-08-08）**：此前`entities`字段的
prompt描述只有"文本中提到的具体地名/店名/景点名，去掉泛称"，没有数量要求，模型抽多抽少完全自由；
且是纯字符串列表，跟原文之间没有保留任何位置/片段级对应关系，事后无法验证某个实体是不是真的
来自原文。这是个真实缺口——`insights.py`的`_passes_grounding_check`依赖`entities`反查LLM生成的
洞察结论是否有依据，但`entities`本身的真实性此前从未被验证过，是建在沙地上的地基。**方案**：
- prompt加穷尽度要求："尽量列出文本中提到的所有具体地名/店名/景点名，不要只挑一两个最显眼的，
  但只能是文本中确实出现过的词，不要因为追求数量而编造或引申"。
- 不改数据结构（仍是`entities: list[str]`），改为入库时加一道**确定性程序过滤**：复用
  `citation_check._entity_matches`（字面匹配+地标后缀剥离，如"武侯祠博物馆"能匹配到原文里
  只写了"武侯祠"的情况）逐个校验每个抽出的实体是否真的在原文里逐字出现，通不过校验的直接从
  列表里剔除（不整条拒绝这份素材，只剔除有问题的那几个词）。
- 权衡判断：穷尽度要求本身的风险已被这道程序过滤大幅对冲——就算模型为了"穷尽"多列了几个边缘
  词，只要不是原文逐字出现的字符串就会被剔除；唯一没被这道校验挡住的残余风险是"抄对了原文
  字面、但语义上不算本地实体"（如提到"跟北京XX店一个风格"时把"北京XX店"逐字摘出来），这类
  语义相关性判断是当前prompt本来就要处理的老问题（跟排除泛称同类），不是穷尽度要求新引入的。
- **数据结构选型说明**（供后续如需重新评估参考）：讨论过改成`{name, quote}`结构（实体+摘录
  原文短句），能提供更强的人工可审查性，但增加了模型出错自由度（`quote`本身也可能被复述走样
  而不是逐字摘抄），且已有`_entity_matches`可以直接复用做校验，不需要新造校验逻辑——综合判断
  选择不改数据结构，只加程序过滤。
- 实现见`src/materials/ingest.py`（`_ContentAnalysis.entities`字段描述+`ingest_topic`里
  `entities = [e for e in analysis.entities if e and _entity_matches(e, content)]`一行），
  mock测试见`tests/test_materials.py::test_ingest_topic_filters_entities_not_in_original_content`。

**规则优先，agent兜底**：能用确定性规则判断的场景（内容长度是否过短、素材是否已采集过）
用规则处理，不需要语义理解；只有"内容是否真的相关"这种要理解语义的判断才交给LLM。
具体实现见 `src/materials/ingest.py`（`_analyze_content`）和
`src/materials/collectors/douyin.py`（desc内容过短的规则判断，自动追加ASR）。

**采集排序阶段的去重原则**：`src/materials/collectors/ranking.py`排序时要先排除`raw/<topic>/manifest.json`
里已存在`source_url`的候选，**在下载/ASR之前**过滤，不要等到`ingest_to_raw`的幂等判断才发现白干了
（那一步已经花完下载+转写成本）。但这条规则**只防"同一个视频被重复抓取"，不防"多个不同视频
覆盖同一话题"**——后者是`8.2节`"多来源印证"设计本身依赖的信号（多个博主独立推荐同一家店，
是在增强可信度，不是冗余），不应该被当成重复过滤掉。真正的去重边界是`source_url`级别的，
不是内容语义级别的。

**采集排序要支持多种排序意图，不能只有一个固定加权公式**：`ranking.py`原来的`score_item`是
播放/点赞/时长/时新单一加权综合分，无法满足"给我最新的"或"给我最火的"这类明确指定单一维度
的用户需求（综合分会被其他维度稀释掉目标维度的排序结果）。排序函数应支持`sort_by`参数区分
"综合"（默认加权）/"最新"（纯按发布时间）/"最热"（纯按播放+点赞等热度信号）几种模式。

**素材获取交给agent自主控制时，有两个检查点不能/不需要自动化掉（已实现，见下）**：
1. **登录态**：抖音/B站的登录cookie过期需要人工扫码。**实现方式比最初设想的更简单**：不做
   额外的"检测失败→暂停→等登录→resume"机制，agent触发的采集工具内部就是普通的
   `subprocess.run()`调用MediaCrawler，登录态过期时子进程本身会同步阻塞弹出扫码窗口，
   有人扫完码子进程自然继续、正常返回结果——不需要在图结构里专门处理这个状态。
2. **批量预算确认**：入库质量门禁（`is_relevant`）是在ASR转写**之后**才生效的判断——如果完全
   无人值守跑，agent可能对着一批实际上没什么价值的候选批量花掉ASR成本才发现该拒绝。**已实现为
   "提案-确认-采集"三节点流程**（`src/agents/material_agent/collection_graph.py`）：agent先
   自主搜索排序去重、调用`propose_candidates`提交候选，`interrupt()`暂停等人工确认（复用
   `await_skeleton_confirm_node`同款的interrupt模式），确认后才真正触发下载/ASR/入库——这是
   成本控制，不是质量判断（质量判断已经自动化），两者性质不同，不应该被质量门禁的存在误认为
   可以省掉。

### 8.2 检索与可信度（RAG查询时现算，不落库）

- 任意agent做RAG检索时，召回的Top-K片段按"实体"字段做分组，统计同一实体覆盖了几个不同来源/作者。
- 这个"N个来源提及"信号是**查询时现算**的，永远反映库里最新全部数据，无需维护全局分数、无需处理"新增素材导致旧标记过时"的问题。
- **MVP简化**：v1阶段可以先不做实体聚类，直接把召回结果的来源/作者多样性透传给下游agent，由agent在生成内容时用自然语言表达置信度（如"多个博主都提到""该信息仅一处来源，建议现场确认"），实体聚类作为v2优化项。

**复用`insights.py`共识判定作为可信度信号（`/grilling`访谈定案，2026-08-07，尚未实现，依赖8.1节
entities质量改进先落地）**：与其在这里重新实现一套聚合统计（上面"MVP简化"提到的v2优化项，至今
未做），不如复用`src/materials/insights.py`已有、且已验证过的共识判定逻辑——`_confidence_and_sources`
统计一条洞察结论的支撑来源里有几个不同作者，≥2个标`confidence="consensus"`，且这套判定本身已经
做过`_passes_grounding_check`接地校验（防止LLM编造doc_ids/结论）。**方案**：
- 前提顺序：必须先完成上面entities质量改进（穷尽度prompt + `_entity_matches`程序过滤），可信度
  信号的可靠性建立在entities本身可信的基础上，方案可以先记录，但实现顺序上排在entities改进之后。
- 桥接方式：`insights.json`是topic级的聚合结论（不是chunk级），跟`retrieve_materials`返回的
  chunk级结果粒度不同，没法直接查"这个chunk在不在insights.json里"。做法是拿检索结果chunk的
  `entities`，逐个用`_entity_matches`去跟`insights.json`里全部结论的`summary`做字面匹配，命中
  一条`confidence="consensus"`的结论，就给这条检索结果附加一个信号（如`consensus_hint`字段）。
- 信号性质：**软信号，锦上添花**——`insights.json`只有素材数≥`MIN_ENTRIES_FOR_INSIGHTS`(=5)时才
  会生成，素材偏薄的目的地大概率查不到或内容稀薄，查不到/匹配不到时不返回任何东西，agent该怎么
  判断还怎么判断，不新增任何"agent必须参考这个信号"的强约束——避免在素材薄的目的地引入新的
  失败模式。
- 一致性：`insights.json`是批量生成的静态文件（只在`ingest_topic`真正有新增条目时重新生成一次，
  见8.1节），跟`retrieve_materials`的实时检索之间存在天然的更新时间差，**不做新鲜度校验**（不比对
  生成时间戳和entries.jsonl最后修改时间）——因为是软信号，最坏情况只是信号滞后一两次入库，不会
  造成错误决策，加新鲜度判断只是徒增复杂度、收益很小。

### 8.3 三层素材/笔记边界（2026-08-26明确）

1. **全局核心素材**：`data/material_library/raw|index/<topic>/`，跨Project共享；只有采集/提交后经过`ingest_topic`质量门禁并生成entry+chunk的内容进入这一层。
2. **Project补充证据**：`projects/<project_id>/materials/evidence-<uuid>.json`，只对当前Project的`retrieve_materials`和引用核验可见，不写入全局库。`web_search_log.jsonl`按单条搜索结果保存`title/excerpt/url`，`append_material`提交的`source_url`必须精确匹配同一Agent本Project的一条结构化结果。记录中的`summary`由Agent提供，只用于检索排序和展示；`evidence_excerpt`及`source_title/source_query/source_observed_at`由系统从匹配结果复制，只有原始标题与`evidence_excerpt`进入引用核验。非route引用即使实体名称已经匹配，只要使用Project evidence仍强制进入实质内容核验，避免Agent补写的非数字定性事实借实体匹配直接放行。采用UUID文件名和同目录临时文件+`os.replace`原子写入；旧`note-*.json`和`project-evidence-v1`不自动迁移、不可引用。
3. **Agent工作笔记**：`projects/<project_id>/agents/<agent_scope>/findings.jsonl`，`note_type=working_note`。用于同一Agent后续重试/跨条目记忆，不进入检索结果、`sources`或最终参考来源表。无可靠来源的推断只能进入这一层。

`append_material`现在只表示第2层Project补充证据；`record_finding`只表示第3层工作笔记。只有全局核心素材原文和Project补充证据的系统捕获摘录能作为引用依据，Agent自己写的`summary`和`working_note`都不能作为引用原文。

---

## 9. 写入权限与版本管理

| 写入对象 | 写入方 | 触发方式 | 是否留痕 |
|---|---|---|---|
| 各agent自己的产出区 | 该agent本人 | 直接写 | 否（可选） |
| 最终攻略（合并稿） | 仅整合agent | 主agent触发 | 是（变更日志） |
| 全局核心素材库 | 核心素材agent/提交与自动补采程序 | 质量门禁后追加 | 是（manifest/entry/chunk可追溯） |
| Project补充证据 | route/lifestyle/POI详情agent（必须有结构化日志中的精确source_url） | 系统复制原始excerpt，UUID文件+原子创建 | 是（actor/source_url/source_query/source_observed_at/created_at/note_type） |
| Agent工作笔记 | 对应Agent自己的`record_finding` | 加锁追加JSONL | 是（actor/created_at/note_type） |
| 公共区（实时信息） | 实时agent（v2） | 用户@触发 → **主agent审批** | 是（变更日志，谁改的/改了什么/何时） |
| 全局用户记忆 | 主agent统一写入 | 任意agent发现新偏好 → 通知主agent → 主agent写入 | 建议是 |

统一原则：**除各agent自己的产出区外，其余公共写入都要经过主agent这一个通路**，避免出现第二个写入入口导致状态不一致。

---

## 10. 全局用户记忆

- 由主agent统一维护和写入，任何agent发现新的用户偏好信息（如"不吃辣"），都通过主agent同步，而不是自己直接写。
- 实时同步而非项目结束后才统一写入，确保尚未执行的下游agent能拿到最新偏好。

---

## 11. 技术栈与实现边界

完整早期技术选型演变见 [`2026-09-04-architecture-sections-11-12-history.md`](2026-09-04-architecture-sections-11-12-history.md#architecture-section-11)。当前实现边界如下：

- 后端和编排使用Python、LangGraph和LangChain；各业务Agent是独立的React Agent，主Agent通过统一图和依赖表调度。
- Project Workspace是业务事实来源，Pydantic Schema和Store负责结构、权限、版本与审计；Checkpoint只负责图执行恢复。
- 模型按full、light和质量门禁等用途分层，具体渠道、Key池和回退行为以`src/config.py`、`src/llm_rotation.py`及运行配置为准。
- 素材链路支持本地原文、B站、抖音、小红书提交、混合检索和限量自动补充；联网、地图、飞书和生图能力依赖外部凭证、额度及服务可用性。
- 用户入口包括CLI和飞书；运维入口由FastAPI与React管理后台提供；静态网站和海报属于呈现层，不进入主生成图。
- 本地运行、依赖和平台差异以 [`DEV_SETUP.md`](DEV_SETUP.md) 为准，当前入口命令以根目录 [`README.md`](../README.md) 为准。

## 12. 当前工程优先级

完整MVP开发路线图已归档到 [`2026-09-04-architecture-sections-11-12-history.md`](2026-09-04-architecture-sections-11-12-history.md#architecture-section-12)，不再作为当前施工指南。当前优先级由真实问题、活跃设计和可复现验证共同决定：

1. 提高引用真实性、路线可执行性和外部动态事实的证据质量，避免用历史单次E2E代表当前状态。
2. 改善长流程Checkpoint恢复、Agent工作单元和上下文预算，减少昂贵节点失败后的重复执行。
3. 用固定3/5/8天场景、真实Provider证据和人工抽查验证新契约，明确Mock、隔离真实模型和完整E2E的力度差异。
4. 完成真实业务正文Patch应用/回滚E2E，并积累MonitoringWindow和Observation运营样本；没有样本时不宣称长期效果成立。
5. 保持文档、代码、Prompt、Workspace和测试同步；具体任务从 [`README.md`](README.md) 选择权威文档和活跃设计。

## 13. 呈现层：静态网站生成与管理后台（当前架构摘要）

完整设计、实施和验证时间线见 [`2026-09-04-architecture-sections-13-17-history.md`](2026-09-04-architecture-sections-13-17-history.md#architecture-section-13)。本节只维护当前架构边界。

### 13.1 用户网站：离线静态生成

- `scripts/delivery/run_generate_site.py` 从当前 Project 的结构化路线、`final_guide.md`和POI详情生成自包含静态HTML；森林页与树木详情使用前端JavaScript切换，不依赖运行时API取数。
- 网站属于呈现层衍生产物，不参与生成正确性状态机；重新生成不得反向改写Workspace事实。
- POI自动图片和管理员上传图片只影响展示，人工上传的Project图片由Workspace权限与API写入链路管理。具体Schema和权限以 [`WORKSPACE.md`](WORKSPACE.md) 为准。

### 13.1.1 网站嵌入陪伴对话（受限客服模式，尚未实现）

当前代码没有站点渠道的陪伴会话入口。已接受的设计边界仍是：只读查询、独立会话、不得暴露Patch审批、提醒管理或素材采集等副作用能力。该设计尚不能作为当前功能使用说明。

### 13.2 管理后台：管理员与运营入口

- 后端位于`src/api/`，前端位于`frontend/`；读取Project、产出、时间线、素材、评测和运维数据时复用现有Store与业务服务，不建立第二套事实源。
- 长耗时操作通过任务框架执行并暴露状态；同步小写操作直接走对应API和Workspace权限。彻底重跑与断点续跑是不同语义，不能混用。
- 当前仍以本地或可信内网个人使用为前提，不把“无鉴权”外推为公开部署安全方案。具体接口、页面和能力以`src/api/`、`frontend/`及对应测试为准。

---

## 14. 飞书渠道（当前架构摘要）

完整历史见 [`2026-09-04-architecture-sections-13-17-history.md`](2026-09-04-architecture-sections-13-17-history.md#architecture-section-14)。当前入口采用WebSocket长连接，不使用Webhook；`run_feishu_worker.py`负责独立Worker，`src/channels/feishu/`负责协议适配、群绑定、意图分类、任务调用、进度和回复。

- 飞书渠道复用`run_channel_start`/`run_channel_resume`及陪伴图，不复制主编排逻辑。
- `chat_id`维护一个活跃Project及历史列表；支持完成后确认发起新旅程，但不支持同时操作多个活跃Project或在群里切回历史Project。
- 核心链路、提醒和海报推送有历史真实群聊验证；各次增量的验证力度仍以对应Issue和测试记录为准。

### 14.1 主动提醒机制

支持创建、查询、修改、取消一次性或固定小时间隔提醒，并保留停机恢复后的时间网格。提醒是用户显式创建的私人待办，不向公开网站渠道开放；复杂Cron式日历规则不在当前范围。

### 14.2 进度可见性与呈现层产物推送

`_ProgressTracker`累积展示阶段、耗时和打回信息；完成后可提供管理后台承载的网站地址，并在用户确认后按需生成和发送海报。网站地址依赖管理后台实际运行，图片生成与发送失败不能阻断主生成结果。

### 14.2.1 陪伴阶段能力可发现性与Agent标识

完成消息、欢迎与帮助入口会展示可用能力；进度和回答使用用户可读的Agent名称。该能力当前事实来源是`src/channels/feishu/service.py`、`bindings.py`及对应测试，不能用更早的飞书核心E2E替代本增量的专项验证。

### 14.3 飞书运维可视化

管理后台提供Worker进程心跳和群绑定只读视图。心跳只能证明进程近期写入成功，不能证明飞书侧WebSocket连接真实可用；当前不提供页面解绑、踢群或完整推送日志。

### 14.4 一个群可发起多次旅程

已实现“单群历史列表 + 一个活跃Project”的顺序旅程模型，并兼容旧绑定格式。新旅程采用保守意图判断和二次确认；并行活跃旅程、对话内切回历史Project不在范围内。

### 14.5 陪伴阶段多Bot直达通道（尚未实现）

当前仓库没有`DIRECT_BOTS`注册表或独立文化Bot凭证入口。文化科普已经进入共享陪伴路由，但仍使用主Bot身份；“同群多个独立Bot身份”只保留为历史设计，不得标记为已实现。

---

## 15. 当前未决与观察项

已解决的旧问题不再继续列为未决：素材实体抽取、Lifestyle结构化Schema、系统性评测机制均已实现。当前仍需关注：

- 网站受限客服模式是否实施，以及公开链接的成本和会话隔离边界。
- Lifestyle单Agent是否需要按子领域拆分；只有真实耗时、上下文和质量数据支持时才调整。
- 飞书多Bot直达通道是否实施，以及逐Bot连接健康和真实@识别验证。
- 素材补充的事务一致性与局部回炉真实网络E2E。具体问题按编号从 [`ISSUES.md`](ISSUES.md) 查询。

---

## 16. 素材缺口检测与限量自动补充机制

完整设计与演进见 [`2026-09-04-architecture-sections-13-17-history.md`](2026-09-04-architecture-sections-13-17-history.md#architecture-section-16)。当前实现位于`src/materials/gap_check.py`、`src/orchestrator.py`和Workspace Store：

- Route引用核验、Lifestyle引用核验和POI详情核验在重试耗尽前可调用共享缺口判断；Pitfall不在当前接入范围。
- 缺口判断先真实检索，再核对实体身份，不以单纯grep或相似度阈值直接判定有无素材。
- 自动采集的Project累计上限为10条，单次事件最多2条，计数保存在`workspace/material_gap_supplement.json`。
- 新素材只有真实进入entry和chunk索引后，才获得一次局部修复与复核机会；该机会不重新进入无限重试循环。
- 当前残余边界包括入库中断的事务一致性，以及部分局部回炉路径缺少完整真实网络E2E；以对应Issue为准。

---

## 17. 系统性评测集

完整设计、首轮实现和历史E2E见 [`2026-09-04-architecture-sections-13-17-history.md`](2026-09-04-architecture-sections-13-17-history.md#architecture-section-17)。当前入口为`run_scenario_eval.py`，场景定义位于`eval/scenarios/`，现有成都、峨眉山、青甘大环线、海口和洱海骑行五个场景。

- 评测同时保存确定性Checklist、LLM Judge结果和人工复核位置，结果写入`eval/runs/`；历史结果只代表当时的代码、Prompt、模型渠道、素材和外部环境。
- 评测按真实生产逻辑运行，可产生模型、搜索和素材采集成本；默认是人工触发工具，不是CI及格线。
- 外部API或网络失败必须归类为执行失败，不能包装成内容质量结论；Judge不能替代人工逐字核对引用和最终可执行性。
- 评测、Monitoring和持续质量演进的更细边界以对应设计文档、代码和测试为准。

---

## 18. 用户主导路线设定：必去地点 / 风格解读 / 完整行程草稿（`/grilling`访谈定案，2026-08-08；已实现，2026-08-09）

> 当前契约摘要。完整设计讨论、实施过程和历史验证见 [`2026-09-04-architecture-sections-18-22-history.md`](2026-09-04-architecture-sections-18-22-history.md#architecture-section-18)。更细的路线写入与门禁契约见 [`ROUTE_AGENT_AND_ROUTE_GATE_CONTRACT_REPAIR_DESIGN.md`](ROUTE_AGENT_AND_ROUTE_GATE_CONTRACT_REPAIR_DESIGN.md) 和 [`ROUTE_FEASIBILITY_GATE_DESIGN.md`](ROUTE_FEASIBILITY_GATE_DESIGN.md)。

### 18.1 现状与问题

用户可以提供零散必去地点、路线风格或完整多日草稿。系统把这些输入转成明确约束交给路线规划，而不是把用户草稿直接当成最终路线或跳过研究、地图核验与结构门禁。

### 18.2 数据结构：`must_visit`（三个子方向共用）

`requirements.json`用`must_visit`保存必去地点；每项必须有`name`，`day`可选。完整多日草稿中能识别的地点及其天数也归一化到该字段。同名地点按最新输入合并，变更后重新进入确认。

### 18.3 触发方式：不主动问，识别到才捕获

需求收集不会把“必去地点”增加为所有用户都必须回答的问题；只有用户主动提到地点、顺序或完整草稿时才抽取和保存。

### 18.4 确认机制：扩展`requirements_gather_node`，不新增图节点

`requirements_gather_node`在写入最终需求前展示抽取出的必去地点和天数，允许用户确认、补充或修正。确认循环复用现有需求收集节点，不增加独立图节点。

### 18.5 风格解读：`style_interpretation`结构化字段

用户提供`style`时，需求抽取同时生成`style_interpretation`，将自由文本归纳为POI密度、停留时长、节奏和路线形态等可执行维度。它是路线规划提示，不替代用户原始风格文本。

### 18.6 `route_agent`侧的消费方式

路线Agent必须消费`must_visit`、指定天数和`style_interpretation`，同时继续基于素材、搜索和地图工具完成完整规划。正式写入模式及字段权限以路线Agent契约和真实Prompt为准。

### 18.7 完全遗漏 vs 排上但不合理：两种不同的处理路径

确定性门禁检查必去地点是否出现以及指定天数是否满足。完全遗漏属于硬约束失败，会阻止确认；地点已安排但存在营业时间、交通或邻接路线等可行性问题时，进入事实修复或结构提案流程，不把两类问题混为同一种失败。

### 18.8 ③完整行程草稿：route_agent仍全权规划，不是纯补全

完整草稿只提供总天数、必去地点、顺序和风格等约束。路线Agent仍负责研究、补全、取舍和形成符合Schema的完整路线。

### 18.9 实施范围

当前实现位于`src/orchestrator.py`、`src/agents/route_agent/`和Workspace Schema/Store，并由路线结构与门禁测试覆盖。当前事实以这些代码、测试和上述专题文档为准。

## 19. 骨架确认环节的健壮性修复 + 检索/入库参数配置化（2026-08-27更新：19.1/19.4/19.5已实现，19.3仍待优化）

> 当前契约摘要。完整历史见 [`2026-09-04-architecture-sections-18-22-history.md`](2026-09-04-architecture-sections-18-22-history.md#architecture-section-19)。

### 19.1 骨架确认无效回复处理（修复）

骨架确认阶段无法识别为确认或修改的回复不会被当成冲突，也不会让流程越过确认点；系统保持等待状态并记录可追溯的变更日志。

### 19.2 定点修改误改检测（`route_edit_overreach`等）触发记录（已实现，2026-08-08）

edit路径会比较请求允许范围与实际改动范围；可疑越权以结构化`issue_type`记录，供后续确认、调试和复核使用。允许范围的动态推断见第20节。

### 19.3 检索/入库链路的硬编码经验参数（待优化项，记录不实现）

部分检索条数、chunk长度和相邻块等经验参数仍散落在实现中，尚未形成统一配置与评测驱动的调优机制。这是明确的技术债，不应被描述为已完成配置化。

### 19.4 骨架确认前的确定性结构与硬约束门禁（已实现，2026-08-27）

路线进入人工确认前必须通过确定性门禁，至少校验总天数、连续Day编号、每日非空POI以及`must_visit`约束。具体可行性类别、证据要求和失败分流以 [`ROUTE_FEASIBILITY_GATE_DESIGN.md`](ROUTE_FEASIBILITY_GATE_DESIGN.md) 为准。

### 19.5 用户确认后的事实修复与结构提案分流（已实现，2026-08-27）

已确认路线后，Agent按`full`、`facts_only`和`proposal_only`三种写入模式分流：事实补丁可在受限字段内修复；会改变天数、POI集合或顺序的结构修改先生成提案，经人工确认后再应用，并只重检受影响天数。

## 20. edit模式误改检测：从静态阈值改为动态语义比对（2026-08-08，`/grilling`访谈定案；20.1/20.2已实现，20.3/20.4/20.5仍为记录/待办）

> 当前契约摘要。完整历史见 [`2026-09-04-architecture-sections-18-22-history.md`](2026-09-04-architecture-sections-18-22-history.md#architecture-section-20)。

### 20.1 问题：静态阈值1不参考用户请求内容

仅按“改动字段或天数超过1个”判断越权，会把用户明确要求的多处修改误报为可疑，因此静态阈值不能作为主要语义判断。

### 20.2 方案：独立轻量LLM判断“用户请求实际涵盖的范围”（已实现）

`_infer_edit_scope`使用轻量模型从用户请求、候选范围和冲突目标推断允许修改集合；`_is_edit_overreach`只在实际改动超出该集合时判定越权。模型判断失败时保守降级到原静态规则，避免检测异常中断编辑流程。

### 20.3 相关澄清：这套检测只在`run_edit`路径生效

该机制只约束直接`run_edit`修改路径。陪伴阶段的只读问答不经过此检测；需要写公共区的实时信息仍走其独立审批与Patch边界。

### 20.4 已记录但本次不处理：route改动级联lifestyle时重新生成范围过宽

路线修改后对Lifestyle的级联仍可能比实际影响范围更宽。这是已知的效率和内容稳定性风险，后续应按依赖字段和受影响天数收窄；未有代码与验证证据前保持为待优化项。

### 20.5 已记录但本次不处理：POI详情agent缺少时效性核验

POI详情的时效性问题现在部分由路线/POI可行性门禁覆盖，但超出已定义可行性类别的动态事实仍是独立范围。当前覆盖边界以 [`ROUTE_FEASIBILITY_GATE_DESIGN.md`](ROUTE_FEASIBILITY_GATE_DESIGN.md)、代码和测试为准，不沿用历史段落中的绝对表述。

## 21. 用户备注/annotation layer（2026-08-21，`/grilling`访谈定案；阶段1/2/3均已实现并真实E2E验证，见21.7）

> 当前契约摘要。完整历史见 [`2026-09-04-architecture-sections-18-22-history.md`](2026-09-04-architecture-sections-18-22-history.md#architecture-section-21)，数据结构和写权限以 [`WORKSPACE.md`](WORKSPACE.md) 为准。

### 21.1 现状与定位

用户备注是用户拥有的annotation layer，用于给行程、某天或某个POI补充私人信息。备注不是Agent生成内容、核验事实或引用来源，不会反向改写路线和攻略正文。

### 21.2 数据结构

备注保存在`workspace/outputs/user_notes.jsonl`。底本按历史追加；修改会创建替代版本，删除会标记旧记录，读取接口只返回当前未删除条目。备注跨`run_edit`和彻底重跑保留。

### 21.3 三入口

网站表单和管理后台API支持新增、修改、删除；飞书入口支持新增。所有写入统一使用人类身份`admin_user`，不伪装成Agent写入。

### 21.4 可见机制

站点生成器按行程、天和POI归类显示备注。绑定对象仍存在时就近展示；对象已从路线消失的孤儿备注退到独立区域，确保文本不丢失。

### 21.5 agent能读（被动，不新增写入路径）

五个陪伴Agent只会收到与当前问题相关的未删除备注，由编排层筛选后注入Prompt。它们可以自然带出备注，但不获得新的备注写工具，也不能把备注当成已核验事实。

### 21.6 权限模型

`WRITE_PERMISSIONS["user_note"]`只允许`admin_user`。Agent权限矩阵不因此扩展；完整Schema、生命周期和权限表由 [`WORKSPACE.md`](WORKSPACE.md) 维护。

### 21.7 实现节奏（分三阶段，每阶段独立可验证）

数据与网站写入、后台管理、飞书新增和陪伴问答注入均已落地。已知边界是实体匹配仍以确定性day/POI识别为主，飞书备注链路的真实环境覆盖弱于网页与管理后台；验证结论应按当前测试和真实运行记录表述，不将历史单次E2E外推为永久保证。

## 22. 生成后定向复核与持续优化闭环（2026-08-22开始实现；2026-09-03当前状态）

> 本节只保留架构入口。完整当前设计、数据模型、状态机和实施矩阵以 [`TARGETED_REPAIR_AND_CONTINUOUS_IMPROVEMENT.md`](TARGETED_REPAIR_AND_CONTINUOUS_IMPROVEMENT.md) 为准；执行器边界见 [`REVIEW_EXECUTOR_OPERATIONS_DESIGN.md`](REVIEW_EXECUTOR_OPERATIONS_DESIGN.md)。历史原文见 [`2026-09-04-architecture-sections-18-22-history.md`](2026-09-04-architecture-sections-18-22-history.md#architecture-section-22)。

### 22.1 事实来源与职责边界

定向复核以既有Project Workspace为事实底本，`src/review/`负责Finding、Recommendation、人工Decision、Patch、影响计划和验收状态。复核系统提出和管理最小修复，不重新生成整份攻略，也不让模型绕过Workspace权限直接写正文。

### 22.2 证据与人工决定

Finding必须关联当前内容、来源证据、Checker结果和版本信息。模型输出只是候选建议；接受、延后、拒绝、应用Patch和启动批次验收均保留人工决定与审计记录。

### 22.3 Patch与影响范围

Patch使用受限JSON Pointer和确定性Store执行，应用前校验目标版本/哈希并原子写入；冲突不得覆盖新内容。已应用Patch可通过新的回滚Patch撤销，原Patch、回滚和后续版本共同保留。影响分类与最小级联计划由确定性策略生成。

### 22.4 当前已实现与边界

后端闭环、Review Center基础页面、Patch提出/应用/拒绝/回滚、影响预览及人工触发的项目级批次验收已经实现。Patch应用后不会自动逐条触发完整下游Agent链；系统累计影响范围，再由运营一次启动定向Checker、必要的一次Integration和最终站点重建。通用可视化diff编辑、跨项目批量处理、路线结构重跑入口、服务端直接启动Codex，以及真实业务正文Patch应用与回滚E2E仍未完成或未充分验证。

## 变更记录

完整版本时间线已归档到 [`2026-09-04-architecture-change-history.md`](2026-09-04-architecture-change-history.md)。本节保留版本检索入口；当前行为请以本文正文、专题权威文档、代码和测试为准。

- [v1.81 · 2026-08-26](2026-09-04-architecture-change-history.md#architecture-change-001)
- [v1.80 · 2026-08-21](2026-09-04-architecture-change-history.md#architecture-change-002)
- [v1.81 · 2026-08-21](2026-09-04-architecture-change-history.md#architecture-change-003)
- [v1.79 · 2026-08-21](2026-09-04-architecture-change-history.md#architecture-change-004)
- [v1.78 · 2026-08-21](2026-09-04-architecture-change-history.md#architecture-change-005)
- [v1.1](2026-09-04-architecture-change-history.md#architecture-change-006)
- [v1.2](2026-09-04-architecture-change-history.md#architecture-change-007)
- [v1.3](2026-09-04-architecture-change-history.md#architecture-change-008)
- [v1.4](2026-09-04-architecture-change-history.md#architecture-change-009)
- [v1.5](2026-09-04-architecture-change-history.md#architecture-change-010)
- [v1.6 · 实现验证阶段](2026-09-04-architecture-change-history.md#architecture-change-011)
- [v1.7](2026-09-04-architecture-change-history.md#architecture-change-012)
- [v1.8](2026-09-04-architecture-change-history.md#architecture-change-013)
- [v1.9 · 设计讨论，实现见`docs/ISSUES.md`第14条](2026-09-04-architecture-change-history.md#architecture-change-014)
- [v1.10](2026-09-04-architecture-change-history.md#architecture-change-015)
- [v1.11](2026-09-04-architecture-change-history.md#architecture-change-016)
- [v1.12 · 设计讨论，未实现](2026-09-04-architecture-change-history.md#architecture-change-017)
- [v1.13 · 设计讨论，未实现](2026-09-04-architecture-change-history.md#architecture-change-018)
- [v1.13 · **已实现**，2026-07-21](2026-09-04-architecture-change-history.md#architecture-change-019)
- [v1.12 · **已实现**，2026-07-21](2026-09-04-architecture-change-history.md#architecture-change-020)
- [v1.14 · 文档核对，2026-07-22](2026-09-04-architecture-change-history.md#architecture-change-021)
- [v1.15 · 2026-07-22](2026-09-04-architecture-change-history.md#architecture-change-022)
- [v1.16 · 2026-07-22](2026-09-04-architecture-change-history.md#architecture-change-023)
- [v1.17 · 2026-07-22](2026-09-04-architecture-change-history.md#architecture-change-024)
- [v1.18 · 2026-07-23](2026-09-04-architecture-change-history.md#architecture-change-025)
- [v1.19 · 2026-07-23，同日](2026-09-04-architecture-change-history.md#architecture-change-026)
- [v1.20 · 2026-07-23，同日](2026-09-04-architecture-change-history.md#architecture-change-027)
- [v1.21 · 补记，2026-07-24](2026-09-04-architecture-change-history.md#architecture-change-028)
- [v1.22 · 2026-07-24](2026-09-04-architecture-change-history.md#architecture-change-029)
- [v1.23 · 2026-07-24](2026-09-04-architecture-change-history.md#architecture-change-030)
- [v1.24 · 2026-07-24](2026-09-04-architecture-change-history.md#architecture-change-031)
- [v1.25 · 2026-07-24，同日](2026-09-04-architecture-change-history.md#architecture-change-032)
- [v1.26 · 2026-07-24，同日](2026-09-04-architecture-change-history.md#architecture-change-033)
- [v1.27 · 2026-07-24，同日](2026-09-04-architecture-change-history.md#architecture-change-034)
- [v1.28 · 2026-07-24，同日](2026-09-04-architecture-change-history.md#architecture-change-035)
- [v1.29 · 2026-07-24，同日](2026-09-04-architecture-change-history.md#architecture-change-036)
- [v1.30 · 2026-07-24，同日](2026-09-04-architecture-change-history.md#architecture-change-037)
- [v1.31 · 2026-07-24，同日](2026-09-04-architecture-change-history.md#architecture-change-038)
- [v1.32 · 2026-07-24，同日](2026-09-04-architecture-change-history.md#architecture-change-039)
- [v1.33 · 2026-07-25](2026-09-04-architecture-change-history.md#architecture-change-040)
- [v1.34 · 2026-07-25，同日](2026-09-04-architecture-change-history.md#architecture-change-041)
- [v1.35 · 2026-07-25，同日](2026-09-04-architecture-change-history.md#architecture-change-042)
- [v1.36 · 2026-07-25，同日](2026-09-04-architecture-change-history.md#architecture-change-043)
- [v1.37 · 2026-07-25，同日](2026-09-04-architecture-change-history.md#architecture-change-044)
- [v1.38 · 2026-07-25，同日](2026-09-04-architecture-change-history.md#architecture-change-045)
- [v1.39 · 2026-07-25，同日](2026-09-04-architecture-change-history.md#architecture-change-046)
- [v1.40 · 2026-07-25，同日](2026-09-04-architecture-change-history.md#architecture-change-047)
- [v1.41 · 2026-07-25，同日](2026-09-04-architecture-change-history.md#architecture-change-048)
- [v1.42 · 2026-07-25，同日](2026-09-04-architecture-change-history.md#architecture-change-049)
- [v1.43 · 2026-07-25，同日，收尾](2026-09-04-architecture-change-history.md#architecture-change-050)
- [v1.44 · 2026-07-26](2026-09-04-architecture-change-history.md#architecture-change-051)
- [v1.44 · 2026-07-25，同日](2026-09-04-architecture-change-history.md#architecture-change-052)
- [v1.45 · 2026-07-25，同日](2026-09-04-architecture-change-history.md#architecture-change-053)
- [v1.46 · 2026-07-26](2026-09-04-architecture-change-history.md#architecture-change-054)
- [v1.47 · 2026-07-26，同日](2026-09-04-architecture-change-history.md#architecture-change-055)
- [v1.48 · 2026-07-26，同日](2026-09-04-architecture-change-history.md#architecture-change-056)
- [v1.49 · 2026-07-26，同日](2026-09-04-architecture-change-history.md#architecture-change-057)
- [v1.50 · 2026-07-26，同日](2026-09-04-architecture-change-history.md#architecture-change-058)
- [v1.51 · 2026-07-26，同日](2026-09-04-architecture-change-history.md#architecture-change-059)
- [v1.52 · 2026-07-26，同日](2026-09-04-architecture-change-history.md#architecture-change-060)
- [v1.53 · 2026-07-26，同日](2026-09-04-architecture-change-history.md#architecture-change-061)
- [v1.55 · 2026-07-26，同日](2026-09-04-architecture-change-history.md#architecture-change-062)
- [v1.56 · 2026-07-26，同日](2026-09-04-architecture-change-history.md#architecture-change-063)
- [v1.54 · 2026-07-26，同日](2026-09-04-architecture-change-history.md#architecture-change-064)
- [v1.55 · 2026-07-26，同日](2026-09-04-architecture-change-history.md#architecture-change-065)
- [v1.57 · 2026-07-26，同日](2026-09-04-architecture-change-history.md#architecture-change-066)
- [v1.58 · 2026-07-26，同日](2026-09-04-architecture-change-history.md#architecture-change-067)
- [v1.59 · 2026-07-26，同日](2026-09-04-architecture-change-history.md#architecture-change-068)
- [v1.60 · 2026-07-26，同日](2026-09-04-architecture-change-history.md#architecture-change-069)
- [v1.61 · 2026-07-31](2026-09-04-architecture-change-history.md#architecture-change-070)
- [v1.62 · 2026-08-01](2026-09-04-architecture-change-history.md#architecture-change-071)
- [v1.63 · 2026-08-01，同日](2026-09-04-architecture-change-history.md#architecture-change-072)
- [v1.64 · 2026-08-01，同日](2026-09-04-architecture-change-history.md#architecture-change-073)
- [v1.65 · 2026-08-01，同日](2026-09-04-architecture-change-history.md#architecture-change-074)
- [v1.66 · 2026-08-01，同日](2026-09-04-architecture-change-history.md#architecture-change-075)
- [v1.67 · 2026-08-01至08-02](2026-09-04-architecture-change-history.md#architecture-change-076)
- [v1.68 · 2026-08-07](2026-09-04-architecture-change-history.md#architecture-change-077)
- [v1.69 · 2026-08-07，同日](2026-09-04-architecture-change-history.md#architecture-change-078)
- [v1.70 · 2026-08-08](2026-09-04-architecture-change-history.md#architecture-change-079)
- [v1.71 · 2026-08-08，同日](2026-09-04-architecture-change-history.md#architecture-change-080)
- [v1.72 · 2026-08-08，同日](2026-09-04-architecture-change-history.md#architecture-change-081)
- [v1.73 · 2026-08-08，同日](2026-09-04-architecture-change-history.md#architecture-change-082)
- [v1.74 · 2026-08-08，同日](2026-09-04-architecture-change-history.md#architecture-change-083)
- [v1.75 · 2026-08-08，同日](2026-09-04-architecture-change-history.md#architecture-change-084)
- [v1.76 · 2026-08-09](2026-09-04-architecture-change-history.md#architecture-change-085)
- [v1.77 · 2026-08-17](2026-09-04-architecture-change-history.md#architecture-change-086)
- [v1.79 · 2026-08-21](2026-09-04-architecture-change-history.md#architecture-change-087)
- [v1.81 · 2026-08-30](2026-09-04-architecture-change-history.md#architecture-change-088)
- [v1.82 · 2026-08-31](2026-09-04-architecture-change-history.md#architecture-change-089)
