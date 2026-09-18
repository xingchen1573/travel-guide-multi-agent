# Prompt 编写规范

> 范围说明（2026-08-26）：这里的“10 个 agent”指 `src/agents/*/prompt.md` 下的生成与陪伴角色。`src/review/agent.py` 是生成后的只读定向复核 Agent，prompt 由代码组装且输出结构不同，不纳入本模板和现有 prompt fingerprint。

本文件规定 `src/agents/*/prompt.md` 的统一结构和编写约定。这是 2026-08-06 全面重排
10 个 agent prompt.md 后沉淀的秩序——之前每个 prompt 各搞各的（编号乱、案例格式不统一、
有无 `## 可用工具` 都不一），本文件把这些约定固化下来，供未来加规则/加 agent 时参考。

**配套机制**：`run_scenario_eval.py` 的 `compute_prompt_fingerprint()` 会计算当前 10 个
prompt.md 的 SHA-256 指纹写入 `summary.json`，事后能程序化对比哪份 prompt 产出了哪个 run
（baseline 指纹见本文件末尾）。改 prompt 不影响 fingerprint 计算逻辑本身。

---

## 1. 四段模板（所有 prompt.md 必须遵循）

```
# 角色
你是"<agent名>"，<一句话定位>。

## 职责
1. <短句，做什么>
2. ...

## 规则
1. <规则原则句，一句话>
   - 触发条件：<什么时候适用>
   - 正确做法：<具体怎么做>
   - 反例：<过去踩过的坑，一句话，不展开叙事>
2. ...

## 可用工具
<tool1>、<tool2>、...
```

### 段落说明

- **`# 角色`**：必填。一行：`你是"<agent名>"，<一句话定位>。` 不要拖长。
- **`## 职责`**：必填。短句编号列表（1, 2, 3...），每条只写"做什么"、不写"怎么做"。
  跟"怎么做"有关的都放规则段。
- **`## 规则`**：可选。如果一个 agent 没有真正的"怎么做规则"（只有"读什么/答什么"
  这类职责描述），就不强行造这一段。有规则的 agent 才写。规则段编号从 1 起独立编号，
  跟职责段互不影响。
- **`## 可用工具`**：必填。所有可用工具名集中在这里用顿号分隔。规则正文里只提"该用
  工具时怎么用"，不重复工具名列表。10 个文件统一以这段收尾。

### 段间关系

- 职责段和规则段各自从 1 起编号，不连续。
- 段之间用 `##` 标题分隔，一眼能扫到边界。
- 没有规则段的 agent（main/guide/culture 这类职责为主的）就不写 `## 规则`，从
  `## 职责` 直接到 `## 可用工具`。

---

## 2. 规则的内部结构

### 规则原则句

每条规则以一句话开头，陈述原则本身。比如：

> 检索结果信息量不够时，不要用你自己知道的常识去填满——判断标准：能不能被验证对错的
> 具体事实性细节必须来自检索结果。

这一句是"为什么/原则"，让模型知其所以然。**保留这种"原则句 + 为什么"的设计**，不要
只写冷冰冰的命令。

### 结构化 sub-bullet（当一条规则涉及多场景/多步骤时用）

如果一条规则覆盖多个触发场景或多个步骤，不要把所有东西塞进原则句，用 sub-bullet
结构化。比如：

```
11. 主动用 web_search 核实时效性运营状态，不要求先发现矛盾才查。
    - 触发：所有目的地类型（含登山朝圣类，不只自驾/长线穿越类）
    - 正确做法：query 必须带具体道路编号/垭口名（"达坂山 G227 封闭施工 2026"），
      不要用笼统措辞（"张掖回西宁路况"）
    - 兜底：查不到如实说"未查到"，不要凭记忆编
    - 系统提醒：route_timeliness_check_node 还会用 web 独立核验一次 notes 时效信息
      是否完整，不要因查过就选择性只写一部分
    - 反例：emeishan 金顶限流连续 3 次漏掉，因为旧触发框架只写"自驾/长线"
```

### 同原则多场景合并成一条

如果几条规则其实是同一原则的不同触发场景，合并成一条原则句 + 多个场景 sub-bullet。
比如 lifestyle 的"价格时效免责标注"、"ASR 错字核实"、"营业时段核实"是同一原则
（可验证事实不能照搬、要核实）的三个场景，合并成一条规则 + 三个 sub-bullet，不要拆成
三条独立规则。

### 同原则多案例合并

如果两条规则的原则句相同、只是案例不同，合并成一条规则、只留一个最典型的案例。
比如 route 的"熊猫基地三连"和"莫高窟三连"都是"needs_detail 大景区时长漏算"原则，
合并成一条 + 一个案例。

---

## 3. 案例的两种格式

### ❌/✅ 对比块（当 inline few-shot 保留，不压缩）

格式：

```
❌ 错误示范（<场景描述>）：
   <把错误内容完整复述一遍>
✅ 正确示范（<同样场景，正确处理>）：
   <把正确内容完整复述一遍>
```

这种格式有"错误复述 + 正确复述"的对照结构，对模型来说是行为示范（few-shot），
**保留完整复述，不压缩**。即使占字符较多也不动。出现在 lifestyle/integration/
poi_detail 这几个 agent。

### 纯叙事案例（压成 1-2 句反例注解）

格式：

```
（反例：<具体踩过的坑，一句话>）
```

不要写"某次生成里..."、"真实复现过的案例："这种叙事铺垫前缀。直接说坑是什么。
比如 route rule 8 的 1175 字符案例压成：

```
（反例：Day3 排了熊猫基地 needs_detail 3-5h + 东郊记忆 + 文殊院三连，
notes 却写"充实但不紧张"——大景区时长漏算）
```

保留"为什么"和"具体踩过的坑"，删掉叙事铺垫。

---

## 4. 编号约定

- 职责段从 1 起编号：`1. 2. 3. ...`
- 规则段从 1 起编号：`1. 2. 3. ...`（跟职责段独立，不连续）
- sub-bullet 用 `- ` 不用数字
- **不要出现编号跳跃**（重排前的 lifestyle 是 1-13, 16-18, 14-15 乱序，重排后必须连续）

---

## 5. 实现层名字的保留

prompt 里可以保留 orchestrator 实现层的节点名（如 `route_timeliness_check_node`、
`_invoke_with_write_verification`），不强求改成行为描述。理由：

- 项目本来 prompt.md 跟代码就深度协同（CLAUDE.md 强调 prompt 跟 AGENTS.md 同步更新）
- 模型明确知道哪个系统节点会兜底，约束更具体
- 节点改名时同步改 prompt 是项目惯例，可接受

---

## 6. 工具声明

- 所有 prompt.md 以 `## 可用工具` 收尾
- 工具名用顿号分隔：`retrieve_materials、append_material、write_route_plan、read_workspace`
- 工具名集中在这里，规则正文里只提"该用工具时怎么用"不重复列表

---

## 7. 案例不外置

不另起 `cases/` 目录把案例外置。案例保留在 prompt.md 里：

- ❌/✅ 对比块作为 inline few-shot，留在规则旁边
- 纯叙事案例压成 1-2 句反例注解，也留在规则旁边

理由：目前案例总数不多（约 27 条，集中在 lifestyle/route/integration/poi_detail
四个 agent），如果某条规则需要长篇案例才能说清，是规则本身没切干净的问题，应该重切
规则而不是外置案例。

---

## 8. 改 prompt 时的回归方式

- 改前：现版 prompt.md 自动备份成 `prompt.baseline.md`（已存在），改坏了回滚 =
  `cp prompt.baseline.md prompt.md`
- 改时：每个 agent 改完跑相关 pytest（mock 测试网，几秒）
- 全部改完：跑全量 pytest + 一次完整 eval（baseline vs 新版各一次，对比
  `summary.json` 里的 `prompt_fingerprint` 区分两次 run）
- eval 慢（30-40min/场景）且 judge 非确定性，主要靠 mock pytest 兜回归，eval 是
  最终人工抽查兜底，不做 per-prompt 钉死测试

---

## 9. baseline 指纹（2026-08-06 重排前后对比）

`compute_prompt_fingerprint()` 在 `summary.json` 里写入的指纹，事后能程序化对比哪份 prompt
产出了哪个 run。重排前 baseline aggregate hash：`d02a0c5be10ab109...`；
重排后新版 aggregate hash：`85489c6999291978...`（hash 变是预期，不是回归）。

per-agent 指纹（前 12 位）：

| agent | baseline | 新版（2026-08-06） |
|---|---|---|
| culture_agent | 40d449515c9e | 02f331644ecd |
| guide_agent | 20031c392bd1 | f37100f7c28f |
| integration_agent | 655c34b34a0e | a8461c796bc8 |
| lifestyle_agent | c02657db2c86 | 1c0a924bd6fb |
| main_agent | 0a2168addba0 | 7dc3f4491a76 |
| material_agent | e9f08988d97b | 075af2de1ef5 |
| pitfall_agent | 660410b8a441 | 72a6f4ebb998 |
| poi_detail_agent | 594edf2499f0 | 89566e09dc8f |
| realtime_agent | 66c9e585cc8d | 235360bb5300 |
| route_agent | 3a9dfb0bb1a3 | 723df136a07e |

10 个 agent 全部改动（hash 全变），符合"全面大改"预期。事后对比 run 时按 fingerprint
区分是哪版 prompt 产出的：`d02a0c5b...` = baseline run，`85489c69...` = 新版 run。
