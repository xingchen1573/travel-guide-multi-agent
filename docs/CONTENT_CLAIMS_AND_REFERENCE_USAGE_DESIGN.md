# 内容事实、来源使用位置与最终攻略引用设计

> 状态：阶段A/B/C均已实现；`direct/amap`实体归属与正文支持合取规则已于2026-09-07补齐，真实运行覆盖与交互效果继续观察
> 调研日期：2026-08-28  
> 适用范围：路线、衣食住行、避雷、POI详情、最终攻略与参考来源展示  
> 相关文档：`OUTPUT_EVIDENCE_AND_CHECKER_EXPLAINABILITY.md`、`AUDIT_OBSERVABILITY_AND_EVALUATION_DESIGN.md`

## 1. 结论

三个阶段完成后，当前系统已经能回答：

```text
某个路线天、衣食住行条目、避雷条目或POI途经点
→ 在自己的sources字段里声明了哪些来源
→ 核验器读取过哪些材料并得到什么结果
```

阶段A启动时尚不能完整回答、现在已由阶段B/C补齐的问题包括：

```text
某条来源具体被哪些产物对象使用
某个对象里每一句具体说法分别由哪条来源支持
最终攻略正文里的某句话来自哪个上游事实
```

因此需要分两步建设，不能把两个问题混成一个：

1. **来源使用位置（`cited_in`）**：回答“这条来源被挂在了哪些对象下面”。这一步可以基于现有数据确定性实现，也能回填历史Project。
2. **事实级声明（`claims`）**：回答“这条来源具体支持对象里的哪一句话”。这一步需要修改各Agent的写入结构、核验器和前端，新生成数据最可靠。

`cited_in`不能代替`claims`。前者是对象级反向索引，后者才是事实到证据的精确映射。

---

## 2. 三个概念

### 2.1 `sources`：对象声明使用的来源集合

以现有Day2美食建议为例：

```text
从成都乘高铁到离堆公园站，午餐可在灌县古城解决，晚上去南桥看蓝眼泪夜景。
```

当前`LifestyleItem`的结构是：

```json
{
  "day": 2,
  "recommendation": "从成都乘高铁到离堆公园站……",
  "sources": [
    {"chunk_id": "成都-020-c001"},
    {"chunk_id": "成都-031-c000"}
  ],
  "claimed_entities": ["离堆公园站", "灌县古城", "南桥"]
}
```

这只能说明整条建议声明使用了两个chunk，不能判断每个chunk分别支持哪一句。

当前代码位置：

- Schema：`src/workspace/schema.py::LifestyleItem`
- 写入工具：`src/tools/workspace_tools.py`中`actor == "lifestyle_agent"`的`write_agent_output`
- 核验入口：`src/materials/citation_check.py::verify_lifestyle_citations`

### 2.2 `claim`：一条可单独判断的具体说法

`claim`不是新的参考来源，而是从长文本中拆出的一条最小可验收事实。例如：

```json
{
  "claim_text": "从成都乘高铁到离堆公园站",
  "claim_type": "transport",
  "support_mode": "direct",
  "evidence_refs": [
    {"chunk_id": "成都-020-c001"}
  ]
}
```

同一条美食建议可以包含多条claims：

```text
“从成都乘高铁到离堆公园站” → 成都-020-c001
“午餐可在灌县古城解决”       → 成都-031-c000
“晚上可去南桥看夜景”          → 成都-020-c001
“具体餐厅现场选择”             → unverified / estimate
```

路线层已经存在`RouteClaim`，字段包括`claim_id/target_path/claim_text/support_mode/evidence_refs/evidence_snapshots/derived_from/provenance_mode`。

当前代码位置：

- Schema：`src/workspace/schema.py::RouteClaim`
- 写入准备：`src/tools/workspace_tools.py::_prepare_route_day_claims`
- 核验入口：`src/materials/citation_check.py::verify_route_citations`
- 前端展示：`frontend/src/pages/AgentOutputsPage.tsx::ClaimsSection`

新路线写入已经强制要求claims，并由claims的`evidence_refs`自动汇总`RouteDay.sources`。截至2026-08-28，仓库现有历史Project的`route_plan.json`尚未发现已落盘的`claims`，因此当前页面主要仍在展示旧数据的天级来源与历史回填检查。

### 2.3 `cited_in`：来源被声明使用的位置

`cited_in`是从来源反向查看使用位置：

```text
成都-020-c001
→ route:day:2
→ lifestyle:food:day:2:item:0
→ lifestyle:play:day:2:item:0
→ poi_detail:day:2:poi:都江堰景区:route:推荐路线:order:9
```

它表达的是：

> 这条来源出现在这些对象的`sources`中。

它不表达：

> 这条来源支持这些对象里的每一句话。

当前`ReferenceEntry`已写入`cited_in: list[str]`和`excerpt`。`collect_cited_chunks()`会从route/lifestyle/pitfall/poi_details的`sources`构造反向使用路径，`build_reference_section()`同步生成Markdown使用位置列和`final_guide.meta.json.references`。

当前代码位置：

- Schema预留：`src/workspace/schema.py::ReferenceEntry`
- 来源收集：`src/materials/citation_check.py::collect_cited_chunks`
- 参考表生成：`src/materials/citation_check.py::build_reference_section`
- 最终覆盖：`src/orchestrator.py::_replace_reference_section`

---

## 3. 当前代码的真实能力

| 模块 | 当前来源粒度 | 当前核验粒度 | 是否有claims |
|---|---|---|---|
| 路线 | 天级`sources`；新代码由claims汇总 | 旧数据天级；新数据事实级 | 已实现，新写入强制要求 |
| 衣食住行 | 新数据由claims汇总`sources`；旧数据保留对象级来源 | 新数据事实级；旧数据item级 | 已实现，新写入强制要求 |
| 避雷 | 新数据由claims汇总`sources`；空warning例外 | 新数据事实级；旧数据item级 | 已实现，非空warning强制要求 |
| POI详情 | 新数据由claims汇总segment `sources` | 新数据事实级；旧数据途经点级 | 已实现，非空途经点字段强制要求 |
| 最终攻略 | 结构化正文块 + 程序渲染Markdown + 全局去重来源表 | 上游claim到正文块的事实映射 | 已实现；旧Project保留自由Markdown兼容 |

截图对应的成都3天Project中，当前共有：

| 对象 | 数量 | `evidence_checks.json`记录数 |
|---|---:|---:|
| 路线天 | 3 | 9 |
| 衣食住行条目 | 12 | 36 |
| 有内容的避雷条目 | 4 | 12 |
| POI途经点 | 45 | 135 |
| 合计 | 64 | 192 |

每个对象通常有“来源可读取、实体对应、具体说法支持”三步记录。这些记录适合管理后台验收，不适合全部平铺在最终攻略阅读页。

---

## 4. 第一阶段：来源使用位置

### 4.1 目标

先解决用户当前最直接的问题：

> 文末有几十条来源，但不知道每条来源在哪些地方被使用。

期望展示：

```text
成都-020-c001 · 被声明使用9处
[Day2路线] [Day2美食] [Day2游玩] [南桥途经点] ……
```

点击位置后，应跳到`AgentOutputsPage`中的对应路线天、衣食住行卡片、避雷卡片或POI途经点。

### 4.2 实施结果

`collect_cited_chunks()`已从原先的“只保留chunk首次出现”改为“来源元数据去重+使用位置累加去重”：

```python
if cid not in seen:
    seen[cid] = source_metadata(source)
cited_in = seen[cid]["cited_in"]
if usage_key not in cited_in:
    cited_in.append(usage_key)
```

历史`web/web_verified`来源也会确定性归一化：泛化的`web`/`web_verified` chunk ID会按URL或文档标识生成稳定ID，`doc_id`中的HTTP URL会恢复到`source_url`，避免多条联网来源被误合并。

### 4.3 建议的数据格式

第一阶段沿用当前`ReferenceEntry.cited_in: list[str]`，不立即引入新的复杂Schema。列表存放与现有`EvidenceCheck.target_key`兼容的稳定路径：

```json
{
  "platform": "douyin_asr",
  "doc_id": "成都-020",
  "chunk_id": "成都-020-c001",
  "cited_in": [
    "route:day:2",
    "lifestyle:food:day:2:item:0",
    "lifestyle:play:day:2:item:0",
    "poi_detail:day:2:poi:都江堰景区:route:推荐路线:order:9"
  ]
}
```

这些路径表达当前产物快照中的位置。衣食住行和避雷目前没有稳定`item_id`，使用`item_index`与现有前端、`evidence_checks.json`保持一致；条目重排后路径可能变化，这是第一阶段的已知边界。

### 4.4 后端改动点

#### A. 收集使用位置

修改`src/materials/citation_check.py::collect_cited_chunks`：

1. 遍历route/lifestyle/pitfall/poi_details时同时构造`usage_key`。
2. 第一次遇到chunk时保存来源元数据并初始化`cited_in=[]`。
3. 后续重复遇到同一chunk时不丢弃，把新的`usage_key`去重追加到`cited_in`。

也可以新增更语义清晰的`collect_cited_chunk_usages()`，再由`collect_cited_chunks()`调用。第二种方案更利于单测，也避免一个函数同时承担过多职责。

#### B. 写入Meta和Markdown

修改`build_reference_section()`：

1. 写入`reference["cited_in"]`。
2. 参考来源Markdown增加“使用位置”或“使用次数”列。
3. 使用位置较多时Markdown只展示前3处和“另N处”，避免表格继续横向膨胀。

`_replace_reference_section()`不需要改变整体职责；它仍然用程序结果覆盖模型可能写出的参考来源章节和`final_guide.meta.json.references`。

`poi_details_join_node()`已经会在POI详情全部生成后重新收集全量引用，因此第一阶段改动会自然覆盖POI层，不需要再增加第三次重建。

#### C. API

当前`GET /api/projects/{project_id}/outputs`已返回`final_guide_meta`和`poi_details`，供前端实现可点击的“来源→使用位置”：

```json
{
  "final_guide_meta": {"references": [...]}
}
```

对应修改：

- `src/api/routers/projects.py::get_project_outputs`
- `frontend/src/api/types.ts::ProjectOutputs`

#### D. 前端

当前`AgentOutputsPage`已实现：

1. 给对应卡片增加DOM anchor。
2. 在整合产出页把全局参考来源从纯Markdown表格升级为结构化来源列表，或在现有预览下新增“来源总表”。
3. 每条来源显示使用次数和位置标签。
4. 点击位置切换到对应主Tab、分类Tab并滚动到对象。
5. 新增POI详情主Tab，可精确跳到具体路线的具体途经点。
6. 移动端收起固定侧栏，用顶部菜单和导航抽屉保持页面可读。

### 4.5 历史数据回填

这一步不需要LLM，也不需要重新生成攻略内容。程序直接读取现有：

```text
route_plan.json
outputs/lifestyle.json
outputs/pitfall.json
outputs/poi_details/*.json
```

即可重建`cited_in`。

建议提供独立维护脚本，例如：

```text
python -m scripts.maintenance.backfill_reference_usages PROJECT_ID
```

回填时只重建程序所有的参考来源章节和meta references，正文主体及POI附录保持不变。测试需要校验正文主体SHA256不变，避免“补来源位置”意外改写攻略。

### 4.6 第一阶段明确不能证明什么

来源使用位置只能证明：

```text
这个chunk出现在这个对象的sources里
```

不能证明：

```text
这个chunk支持对象中的全部文字
Agent生成时确实逐字依赖了这个chunk
不存在遗漏来源
```

前端文案应使用“声明使用位置”，不要使用“支持了这些内容”。

---

## 5. 第二阶段：把claims扩展到其他模块

### 5.1 为什么仍然需要claims

第一阶段完成后可以回答：

```text
成都-020-c001被挂在Day2美食建议下面
```

但仍不能回答：

```text
它支持了高铁、午餐、南桥夜景中的哪一句
```

只有把长内容拆成claims才能回答第二个问题。

### 5.2 Schema改造

路线现有`RouteClaim`已经包含通用字段。建议先抽成公共基类，保留`RouteClaim`名称兼容现有JSON和前端：

```python
class EvidenceClaim(BaseModel):
    claim_id: str
    target_path: str
    subject: str = ""
    claim_text: str
    claim_type: str = "other"
    support_mode: Literal[...]
    claimed_entities: list[str]
    evidence_refs: list[SourceRef]
    evidence_snapshots: list[EvidenceSnapshot]
    derived_from: list[str]
    provenance_mode: Literal["live", "reconstructed"]

class RouteClaim(EvidenceClaim):
    pass
```

随后增加：

```python
class LifestyleItem(BaseModel):
    ...
    claims: list[EvidenceClaim] = []

class PitfallItem(BaseModel):
    ...
    claims: list[EvidenceClaim] = []

class RouteSegment(BaseModel):
    ...
    claims: list[EvidenceClaim] = []
```

现有`sources`继续保留，用作兼容字段和对象级快速展示，但新数据由claims的`evidence_refs`自动取并集，不再要求模型同时维护两份来源。

### 5.3 稳定ID的实施边界

当前lifestyle/pitfall的`target_key`包含`item_index`，POI依赖`route_name + order`。编辑、重排后这些位置可能变化。

本阶段已为每条claim生成确定性`claim_id`。生成种子包含所属对象定位、`target_path`、`claim_type`和`claim_text`，因此claims仅重排时ID不变。

以下对象级ID本阶段未强行引入：

```text
LifestyleItem.item_id
PitfallItem.item_id
RouteSegment.segment_id
```

原因是lifestyle/pitfall当前就以`field+day`唯一替换，POI的`order`又是可执行路线语义的一部分。现阶段evidence checks绑定稳定`claim_id`，对象anchor仍使用现有index/order路径。如果后续要支持任意拖拽重排且保留对象级历史，再单独增加上述ID和迁移逻辑。

### 5.4 写入工具改造

当前路线使用`_prepare_route_day_claims()`完成：

- claim_id生成
- chunk_id归一化
- direct/amap来源约束
- Web证据必须先固化
- 原文快照生成
- sources并集生成
- target_path覆盖检查
- derived依赖解析
- 新写入的每条claim必须显式提交`support_mode`；不再因模型漏字段默认猜成`direct`

Schema中的历史默认值仍保留，只用于读取旧Project；新写入由工具层显式拒绝缺失
`support_mode`的claim。工具层不使用“建议/适合”等关键词自动改分类：语义分类仍由产出
Agent负责，核验器负责判断声明的支持类型与证据是否匹配。对核验失败的POI
Claim，局部返修输入必须携带`claim_id/claim_text/target_path/support_mode/reason`，
避免只向模型提供“这个途经点有问题”的模糊摘要。

第二阶段已提取公共函数：

```text
_prepare_evidence_claims(project_id, topic, claims, namespace, owner_parts, required_paths, ...)
```

再由三个领域准备函数调用：

- `_prepare_lifestyle_item_claims`
- `_prepare_pitfall_item_claims`
- `_prepare_route_segment_claims`

路线层仍保留现有`_prepare_route_day_claims`以降低回归面，Schema已通过`RouteClaim(EvidenceClaim)`共享通用字段。

共同调用。

需要修改的实际工具入口：

- `src/tools/workspace_tools.py`中lifestyle的`write_agent_output`
- 同文件pitfall的`write_agent_output`
- 同文件poi_detail的`write_agent_output/write_agent_output_patch`
- 对应Agent的`prompt.md`

#### 5.4.1 Claim原子性与`claimed_entities`边界

一条Claim的验收单位必须足够小，使它能够独立通过、失败或被暂缓。不能把地点身份、
亮点、价格、时刻、游览顺序和路线判断揉成一句`direct` Claim。例如：

```text
“寄存免费、直通车20元且9:00发车”
→ 寄存事实、价格事实、发车时刻三条Claim

“华藏寺是重要看点，适合纳入本次礼佛主线”
→ 外部看点事实 + 本次路线编排判断两条Claim
```

`claimed_entities`不是从`claim_text`抽取的关键词列表，只承载该Claim实际指向的具体
对象，例如景点、内部点位、建筑、展陈或有专名的线路。时间、顺序、动作、价格、玩法和
普通交通类别仍写在`claim_text`中，不得拼成`七彩丹霞北门观光车顺序`、`万马饮水时间`
一类伪实体；`景区直通车/观光车/接驳车`本身也不是线路专名。复合表述只保留真实对象，
例如分别使用`七彩丹霞景区北门`、`万马饮水`。无证的精确开放、发车或票价事实不能用
`estimate`兜底，应删除或标为`unverified`等待复核。

别名核验只回答“当前上下文是否指向同一现实对象或同一被描述对象”，不要求完整标签连续
逐字出现。繁简体、正式全称/简称、上下文明确的交通称呼和ASR错字可以进入别名消歧；
不同入口、不同景点、不同城市或仅主题相关仍必须失败。价格、时间、顺序和玩法是否得到
原文支持，继续由后续实质内容核验判断，不能借别名消歧放宽。

2026-09-07起，`direct`和`amap` Claim的最终支持结果明确改为三关合取：

```text
证据可读
+ 实体归属当前Claim
+ 正文实质支持该说法
= 可通过
```

任一关出现`fail/error/inconclusive`都生成同一条可追溯issue；`point_name`等明确标记为`entity_identity_only`的身份Claim仍只要求证据可读+实体归属，不虚构一次正文实质支持判定。特别是，正文出现了相似文字只能证明“这句话在某份材料里出现”，不能覆盖“证据实际属于另一个POI”的实体失败。该规则同样用于符合指纹的历史核验复用，不会因复用旧`substantive_claim=pass`而跳过已记录的`entity_identity=fail`。

ASR别名继续可以用于发现可能的同音/转写关系，但对引用ASR来源的`direct/amap` Claim，别名Judge本身不再充当规范名称证据。若要把ASR体验内容归到纠正后名称，还需要该Claim实际引用的官方或其他非ASR原文真实支持该规范实体名；仅在`sources` 中混入一条无关非ASR来源不能放行。否则保留为返修、`force_finalized`或人工复核。这不引入地名字典，也不允许借同segment的兄弟Claim自动共享证据。

该边界由生产POI Prompt和Codex POI Role共同声明；当前不增加关键词分类器、景区白名单
或新质量Agent。写入层继续负责Schema、来源可读性和Claim级隔离，语义遵循效果由冻结样本
和真实模型专项验证观察。

### 5.5 每类对象的必填事实路径

不应该要求把每个形容词都拆成claim，只要求覆盖可验证、会影响用户决策的内容。

| 对象 | 建议强制覆盖 |
|---|---|
| LifestyleItem | 具体店名/菜名、价格、时间、交通、住宿位置、可执行游玩策略 |
| PitfallItem | 具体风险对象、风险事实、触发条件、时间或价格等数字 |
| RouteSegment | `point_name`、`highlight`中的具体事实、非空`suggested_duration` |
| 通用建议 | 用`estimate/user_constraint/unverified`明确性质，不强行伪造外部来源 |

### 5.6 核验器改造

当前三个入口都把整个对象拼成一条`_verify_items()`输入：

- `verify_lifestyle_citations()`核验整条`recommendation`
- `verify_pitfall_citations()`核验整条`warning`
- `verify_poi_detail_citations()`核验`point_name + highlight + suggested_duration`

改造后：

1. 如果对象有claims，按claim逐条构造核验item。
2. `target_key`加入稳定`claim_id`。
3. 每条claim只能读取自己的`evidence_refs/evidence_snapshots`。
4. 不允许借同对象其他claim的来源通过核验。
5. 对没有claims的旧对象继续走现有整条item核验，保持兼容。

`evidence_checks.json`已有`claim_id/claim_text/claim_type/support_mode/target_path`可选字段，能够承载扩展后的结果，不需要新建第二份检查文件。

### 5.7 前端改造

当前`ClaimsSection`已经能展示：

- 事实文本
- 支持方式
- 生成时证据快照
- 推导依赖
- 每一步核验

已将它泛化为`ClaimsSection`，在路线、衣食住行、避雷和POI途经点复用。旧对象`claims=[]`时不渲染空面板，继续显示原有对象级来源与核验。

页面层级保持：

```text
对象卡片正文
→ 关键事实摘要（默认可见）
→ 单条事实详情（折叠）
→ 完整核验步骤（继续折叠）
```

不应把截图Project的192条检查全部默认展开。

### 5.8 历史数据边界

旧对象只有一段内容和一组sources，无法确定生成时每句话实际使用了哪条来源。

历史处理分两档：

1. **确定性回填**：只生成第一阶段的对象级`cited_in`，可信。
2. **模型重建claims**：可以作为后续专项，但必须标记`provenance_mode="reconstructed"`，并明确“基于当前内容和当前可读证据推断”，不能展示成live生成链路。

---

## 6. 第三阶段：最终攻略正文引用

### 6.1 已实施结构

`integration_agent`的写入工具已从`write_final_guide(content_md, meta)`改为`write_final_guide(blocks, meta)`。每个块都必须选择信息性质：

- `upstream`：使用上游事实，必须填真实`upstream_claim_ids`。
- `editorial`：纯标题、排版、过渡和不引入新事实的清单结构。
- `user_constraint`：直接来自用户硬性要求的表述。
- `unverified`：无上游claim支持但必须保留的不确定提示，必须填`mapping_note`。

程序只接受claim ID作为模型输入，忽略模型自填的`block_id/claim_snapshots`，从当前上游产物重新取值。因此整合Agent不能伪造claim文本、证据或使用位置。

### 6.2 落盘结构

`workspace/final_guide.blocks.json`形如：

```json
{
  "blocks": [
    {
      "block_id": "guide-day2-food",
      "section": "day2.food",
      "markdown": "午餐可在灌县古城解决……",
      "mapping_mode": "upstream",
      "upstream_claim_ids": ["lifestyle-claim-xxx"],
      "claim_snapshots": [{"claim_id": "lifestyle-claim-xxx", "source_path": "outputs/lifestyle.json:/food/items/0/claims/0", "usage_key": "lifestyle:food:day:2:item:0", "claim_text": "……"}]
    }
  ]
}
```

程序按块顺序渲染`final_guide.md`正文，同时保留程序所有的POI附录和参考来源附录。`rendered_body_sha256`与当前Markdown正文必须一致；直接改写Markdown后旧blocks会被视为过期，API不再展示。

### 6.3 交付契约与页面

`validate_final_guide_contract()`现在额外检查：

- 上游已有claims时必须有正文块映射。
- blocks渲染正文与`final_guide.md`正文及SHA256一致。
- 每个upstream块引用的claim仍存在，且快照与当前上游内容一致。
- Meta的`body_source/blocks_count`与实际文件一致。

管理后台的“整合产出”页增加“正文事实映射”，按块展示映射类型、claim文本、支持方式和上游JSON路径，并可跳回对应的路线/衣食住行/避雷/POI对象。旧Project没有blocks时显示历史数据边界，不猜测映射。

---

## 7. 页面分层

### 7.1 最终攻略阅读页

只展示轻量信息：

```text
晚上前往南桥欣赏夜景〔3〕。
```

点击标记打开证据抽屉，展示事实、结论、原文、来源和使用位置。详细核验步骤默认折叠。

### 7.2 Agent产出验收页

保留当前“人工验收：依据与核验脉络”，并逐步升级为claim级展示。这一页承担审计职责，可以显示完整方法、边界和历史回填标记。

### 7.3 来源总表

文末长表改名为：

```text
引用来源总表（按chunk去重）
```

明确展示：

- 来源平台与原文
- 原始链接
- 声明使用次数
- 声明使用位置
- 是否有不可读来源

全局表只表示引用库存，不冒充逐句引用关系。

---

## 8. 实施顺序

### 阶段A：来源使用位置

范围：

- 收集`cited_in`
- 写入meta references
- 参考表显示使用次数
- API返回final guide meta
- 前端位置跳转
- 历史Project确定性回填

风险较低，不改变Agent生成结构和核验结论。

### 阶段B：通用claims

范围：

- 提取公共EvidenceClaim
- lifestyle/pitfall/POI增加claims和稳定claim ID
- 复用路线claim写入准备逻辑
- checker按claim核验
- 前端复用ClaimsSection
- 旧数据兼容

这是核心真实性改造，需要完整单测和至少一次真实E2E。

### 阶段C：最终正文映射

范围：

- 结构化正文块
- 上游claim到最终正文的映射
- 正文轻量引用标记
- Integration新增事实的来源门禁

改动面最大，单独设计和验收。

---

## 9. 第一阶段测试清单

建议至少增加：

1. 同一chunk跨route/lifestyle/pitfall/poi_details使用时，`cited_in`完整且去重。
2. 同一对象重复写同一source时，使用位置不重复。
3. POI join重建参考表后，原有使用位置保留、POI位置补齐。
4. `build_reference_section()`生成的meta reference包含`cited_in`。
5. 历史web/web_verified来源经过canonicalize后仍保留正确使用位置。
6. 回填脚本执行前后最终攻略正文主体SHA256不变。
7. API返回`final_guide_meta.references[].cited_in`。
8. 前端能从来源位置切换Tab并滚动到对应对象。
9. 无法定位的历史路径显示普通文本，不产生错误跳转。

---

## 10. 第二阶段测试清单

建议至少增加：

1. 新对象有需要核验的具体内容却未提交claims时拒绝写入。
2. claims的来源自动汇总为对象`sources`。
3. direct/amap事实没有可读证据时拒绝写入。
4. 一条claim不能借另一条claim的来源通过。
5. suggested_duration有具体数字时必须有claim或明确标estimate/unverified。
6. patch只替换本次对象的claims和checks，不清空其它天或其它路线。
7. 旧对象无claims时继续走现有item级核验和历史展示。
8. reconstructed与live在前端有明确标签。
9. claims重排后稳定ID仍能关联原核验结果。
10. 新写入漏填`support_mode`时明确拒绝，不影响历史Project读取。
11. POI复合Claim失败时，局部返修Prompt包含精确Claim身份、原句、当前分类和失败原因。
12. POI Prompt明确要求多事实拆分，并禁止把时间、顺序、动作和普通类别写成实体。
13. 别名消歧允许上下文同指和常见转写差异，但不同入口、不同景点和不同城市仍保持失败。
14. 单个子Claim失败时只进入withheld集合，其余同一途经点Claim仍可交付且leak为0。
15. `direct/amap`在“来源可读、实体归属、正文支持”任一层失败时都不得通过，并覆盖“实体fail+正文pass”的跨POI反例。
16. 存在ASR别名纠错时，别名Judge不能单独证明纠正后的规范名；必须在该Claim引用的非ASR原文中实际找到规范实体名，无关非ASR来源不算独立证据。

---

## 11. 本轮实施结果

阶段A已实施：

```text
现有sources正向关系
+ 程序生成的cited_in反向关系
+ 来源总表使用位置与跳转
```

截图对应的成都Project已确定性回填：

- 60条独立来源，115个声明使用位置，24条来源被多处使用。
- 22条为运行时联网证据，其中8条从历史`doc_id`恢复了可点击URL。
- 最终攻略主正文和POI附录与回填前逐字一致，二次执行`changed=false`。
- 桌面与390px移动端已验证来源总表、Tab切换、滚动定位与高亮。

阶段B已实施：

- 抽取`EvidenceClaim/EvidenceClaimEvidenceSnapshot`，`RouteClaim`保留兼容类名。
- lifestyle、pitfall和POI segment新写入接入claims，顶层`sources/claimed_entities`由claim并集生成。
- direct/amap可读证据、Web精确固化、target path覆盖和derived依赖由写入工具确定性校验。
- 新写入要求显式`support_mode`；POI局部返修按失败Claim原句和当前分类输入，
  并重申外部事实与路线编排必须拆分。该项已有确定性回归，新E2E效果待验证。
- 核验器有claims时按claim检查，每条只读自己的证据；历史无claims对象继续走旧的item/segment级核验。
- `direct/amap`的证据可读性、实体归属和正文实质支持已改为最终合取；跨POI实体失败不再被文字相似覆盖，只有ASR来源时不自动确认纠正后规范名。
- 前端复用`ClaimsSection`展示事实、支持方式、证据快照和逐步核验。
- 新增后端claims行为测试与前端复用/历史兼容测试。

阶段C已实施：

- 新增`FinalGuideBlocks/FinalGuideBlock/FinalGuideClaimSnapshot`和`final_guide.blocks.json`。
- Integration写入工具改为提交有序blocks，程序反查claim、固化快照并渲染Markdown。
- 交付契约增加缺失映射、悬空claim、过期正文/快照和Meta不一致检查。
- API只返回与当前正文SHA256一致的blocks；前端按块展示并支持跳回上游对象。
- 旧项目可继续使用自由Markdown，标记为`legacy_markdown`，不伪造事实映射。

后续可用真实页面继续观察：

- 用户最常点击哪些使用位置。
- 每类Agent的实际claim拆分数量是否过密或过粗。
- `estimate/unverified`比例是否揭示了高价值素材缺口。
- POI详情大量途经点生成后的核验密度和页面扫读成本。

---

## 12. 调研参考

- [W3C PROV-O](https://www.w3.org/TR/prov-o/)：区分内容实体、生成/核验活动、责任主体与派生关系。
- [NIST Four Principles of Explainable AI](https://www.nist.gov/publications/four-principles-explainable-artificial-intelligence)：解释应伴随输出、对用户有意义、准确反映过程并说明能力边界。
- [Anthropic Citations](https://platform.claude.com/docs/en/build-with-claude/citations)：引用返回具体`cited_text`和原文位置，引用粒度取决于内容分块。
- [ALCE](https://aclanthology.org/2023.emnlp-main.398/)：把引用正确性与引用完整性作为不同指标评估。
