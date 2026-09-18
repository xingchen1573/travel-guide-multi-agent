# src/materials/collectors/ 素材采集编排

把 sandbox 下已有的独立环节（B站：`bilibili_search` / `bilibili_to_mp3` / `mimo_asr`；抖音：`MediaCrawler` / `douyin_to_text`）串成采集链路，产出规范化落进正式素材库 `data/material_library/raw/<目的地>/`。

**这是"半自动"，不是全自动**：脚本负责搜索、排序、下载、转写、写库，但保留两道人工关口：
1. **勾选关口**：搜索排序后由人选择采集哪些视频（`--auto-top` 可跳过，但更要审核结果）——被素材agent调用时，这道关口体现为`propose_candidates`工具提交候选后需要人工确认，见`docs/ORCHESTRATION.md`素材agent采集图一节。
2. **审核关口**：落库后、`ingest` 前，转写质量（ASR 复读/幻觉/BGM歌词，见下方"ASR不总能提取到有效内容"）里明显的问题会被入库时的`is_relevant`质量门禁自动拦截一部分（见`src/materials/ingest.py`），但不保证100%，仍建议偶尔人工抽查`raw/`内容。

【转正记录】本目录原是 `sandbox/pipeline/`，经过多轮真实验证+单元测试后正式迁入 `src/materials/collectors/`，是"核心素材agent"（`src/agents/material_agent/`）采集能力的直接依赖，不再是探索性脚本，见`docs/ISSUES.md`相关记录。`bilibili_to_mp3`/`mimo_asr`/`MediaCrawler`/`douyin_to_text`这些真正独立单一用途的工具脚本仍留在`sandbox/`，被本目录下的代码以subprocess方式调用。

## 文件

| 文件 | 职责 | 联网 | 可单测 |
|---|---|---|---|
| `ranking.py` | B站搜索结果的硬过滤+排序（综合/最新/最热三种模式，字段来自实测的 search 返回），支持翻页(`page`参数) | 否 | 是（`tests/test_collectors_ranking.py`，12个） |
| `douyin_ranking.py` | 抖音搜索结果的硬过滤+排序（综合/最新/最热三种模式，字段来自实测的 search 返回） | 否 | 是（`tests/test_collectors_douyin_ranking.py`，10个） |
| `ingest_to_raw.py` | 把转写好的一条素材规范化写进 `raw/<目的地>/`：生成 `_原文.md` + append manifest，幂等，platform 校验；`remove_from_manifest` 结构化删除，避免手改manifest.json | 否 | 是（`tests/test_collectors_ingest_to_raw.py`，9个） |
| `bilibili.py` | 编排：search → 排除库中已有 → rank → 人工勾选 → 下音轨 → ASR转写 → `ingest_to_raw`；`collect_one(topic, candidate)`是单条采集的核心函数 | 是 | 是（`tests/test_collectors_bilibili.py`，5个，纯逻辑部分） |
| `douyin.py` | 编排：`trigger_search`触发关键词搜索 → `list_ranked_candidates`读取+排序+去重展示 → `collect_one(topic, source_url)`抓详情+落库，desc过短自动补ASR | 是 | 是（`tests/test_collectors_douyin.py`，7个） |

## 排序信号

**B站**（`ranking.py`）：综合分 = 0.4·播放 + 0.3·点赞 + 0.2·时长适配 + 0.1·时新性（播放/点赞用 log 归一化避免头部拉爆）。
- **硬过滤**：时长 <3min 或 >30min 丢（前者信息量不足，后者 ASR 易退化+成本高）；发布 >2年 丢。
- **时长适配**：5~20min 给满分，两侧线性衰减（深度攻略最优区间）。
- `search_and_rank`支持`page`参数翻页——素材agent候选数不够时会换关键词或翻页重新搜索，不是查一次不够就将就，见`docs/ISSUES.md`第14条。

**抖音**（`douyin_ranking.py`）：综合分 = 0.35·点赞 + 0.2·评论 + 0.2·分享 + 0.15·收藏 + 0.1·时新性。
- **硬过滤**：desc完全为空丢（没有可判断内容）；发布 >2年 丢。
- 没有时长字段，不做B站那种时长过滤/适配打分——抖音短视频本身就短，不需要这层筛选。

两者都支持`sort_by`参数：`综合`（默认加权）/ `最新`（纯按发布时间）/ `最热`（纯按互动信号，不看时新度），满足"给我最新的"/"给我最火的"这类明确指定单一维度的需求，见`docs/ISSUES.md`第14条。

**去重预过滤**：`bilibili.py`的`search_and_rank`和`douyin.py`的`list_ranked_candidates`都会先排除`raw/<topic>/manifest.json`里已有`source_url`的候选，避免对着库里已有的视频重新下载+ASR白白浪费成本。这条规则**只防"同一视频重复抓取"，不防"多视频覆盖同一话题"**——后者是多来源印证信号的一部分，不应该被过滤掉，见`docs/ARCHITECTURE.md`第8.1节。

## 用法

```bash
# B站-交互式：多关键词搜索、排除库中已有、列出排序后候选、手动输入序号采集
PYTHONIOENCODING=utf-8 python -m src.materials.collectors.bilibili 成都 \
    --keyword "成都旅游攻略" --keyword "成都三日游" --top-n 10 --sort-by 综合

# B站-非交互：直接采集排序后前 N 个（批量，但更需事后审核），--sort-by 最新/最热可选
PYTHONIOENCODING=utf-8 python -m src.materials.collectors.bilibili 成都 \
    --keyword "成都美食" --auto-top 3 --sort-by 最热

# 抖音-触发一次关键词搜索（--keywords CLI参数，实测可用，见docs/ISSUES.md第11d条）
PYTHONIOENCODING=utf-8 python -m src.materials.collectors.douyin 成都 \
    --trigger-search --keyword "成都旅游攻略"

# 抖音-列候选：读取本地search结果，排除库中已有、排序展示（不联网，不下载）
PYTHONIOENCODING=utf-8 python -m src.materials.collectors.douyin 成都 \
    --list-candidates --top-n 10 --sort-by 最热

# 抖音：给定挑中的视频链接/aweme_id，抓desc+章节导览
PYTHONIOENCODING=utf-8 python -m src.materials.collectors.douyin 成都 \
    https://www.douyin.com/video/7634530766289915136

# 抖音：强制对全部指定链接补语音转写（默认desc过短会自动补，不需要手动加这个参数）
PYTHONIOENCODING=utf-8 python -m src.materials.collectors.douyin 成都 \
    https://www.douyin.com/video/7634530766289915136 --asr
```

采集完成后，入库这一步（含`is_relevant`质量门禁）：

```bash
PYTHONIOENCODING=utf-8 python -m src.materials.ingest 成都
```

## platform 约定（关键）

B站ASR产出一律用 `platform=bilibili_asr` 写入——ASR 产出是纯 flat 无时间戳文本，
**绝不能标成 `bilibili`**（那是带 `发言人 00:00` 标记的存量格式，flat 文本用它会退化成
一个巨型 chunk，见 `data/material_library/README.md` 平台对照表 和 `docs/ISSUES.md` 第10条）。
`ingest_to_raw` 里有硬校验，标错会直接抛错。

## 幂等键是 (source_url, platform)，不是只看 source_url

`ingest_to_raw` 判断"是否已采集过"用的是 `(source_url, platform)` 联合键。**同一个视频的
desc(`platform=douyin`)和ASR转写(`platform=douyin_asr`)是两条信息量不同的独立素材**（实测下来
ASR明显更丰富——具体店名/路线desc里往往没有，只在语音里提到），两者可以共存，不会被
"同一个source_url=已经采集过"误判掉。文件名撞名时`_disambiguate_filename`会自动加
source_url短标识区分，不会互相覆盖（见`docs/ISSUES.md`第11c/11e条）。

## 抖音

抖音有三种采集方式：

1. **`--type detail`**（`douyin.py`默认CLI模式）：给定具体链接/aweme_id抓详情。
2. **`--type search`**（`trigger_search()`函数封装了`--keywords`CLI参数调用，实测可用，见`docs/ISSUES.md`第11d条）：
   关键词批量搜索候选，抓到后用`list_ranked_candidates()`读取本地`data/douyin/jsonl/search_contents_*.jsonl`，
   自动排除库中已有素材、排序展示（`douyin_ranking.py`，见`docs/ISSUES.md`第14条），不再需要人工从jsonl里翻。
   **`trigger_search`会显式传`--enable_get_meidas false`，只抓元数据不下载视频**——search阶段是给人工/agent
   挑候选用的，排序字段（desc/点赞数/发布时间等）都是纯元数据，不需要视频本体。真实踩过这个坑：不关掉这个开关，
   MediaCrawler会在search时就完整下载每条候选的视频，而且不检查文件是否已存在就会重复下载，agent自主换关键词
   重搜命中大量重复候选时，整个流程会卡在重复下载几十分钟，见`docs/ISSUES.md`第16条。
3. **`collect_one(topic, source_url)` / `collect_many(topic, source_urls)`**：单条/批量完整采集
   （抓详情+落库+desc过短自动补ASR），是素材agent采集图节点直接调用的核心函数。这一步走的是
   detail模式（`fetch_metadata`），**保持`ENABLE_GET_MEIDAS`默认值`true`**——真正采集时需要
   下载视频本体供ASR转写，和search阶段的目的不同，不要混淆两者的开关设置。**多条候选务必用
   `collect_many`，不要循环调用`collect_one`**——每次调用`fetch_metadata`都会独立启动一次
   MediaCrawler子进程+一次浏览器窗口，`collect_many`把多条候选合并成一次`fetch_metadata`调用
   （批量补ASR同理，合并成一次`run_asr_and_ingest`调用）。真实E2E验证时发现过`material_collect_node`
   逐条调用`collect_one`导致2条候选开了2次浏览器窗口，见`docs/ISSUES.md`第16条。

**抖音默认不做ASR**：实测抖音结构化接口的`video_text`（真字幕轨）几乎总是空的，`desc`+`chapter_list`
（UP主手动打的章节标记，如"00:30 交通"）已经是可靠信息来源，`build_douyin_content`会弹性拼接
（有章节就加导览，没有就只用desc）。默认走 `platform=douyin`（generic chunker）。

**语音信息多、desc信息量不够的视频，会自动补语音转写**（`platform=douyin_asr`，走`chunk_flat_transcript`）。
本地mp4已下载过的话（`sandbox/MediaCrawler/data/douyin/videos/<aweme_id>/video.mp4`），
直接抽音轨+转写即可，不用重新下载。

**⚠️ ASR不总能提取到有效内容**：真实遇到过一条视频（纯vlog剪辑配BGM），ASR转写结果是
**背景音乐歌词**而不是真人讲解——这种情况不能落库，落进去是纯噪音，会污染检索排序（重演
`docs/ISSUES.md`第9条"垃圾chunk污染排序"的教训）。补ASR前先看一眼转写结果是不是有实质
攻略内容，不是"转出文字就一定能用"。

## 隐私

manifest 只存 `author`（博主公开昵称/UP主名，是最终报告"参考来源"要展示的内容），
不存任何原始数字 uid/mid。MediaCrawler 侧已对 uid/昵称做脱敏（`anonymize_user_id`/`mask_nickname`），
接抖音时要保留这个脱敏，不把 PII 落进素材库。

## 抖音登录态

抖音需要有效登录态才能抓取，MediaCrawler有两种模式（`sandbox/MediaCrawler/config/base_config.py`的`ENABLE_CDP_MODE`），
两套cookie完全独立、互不相通，且都会过期（约一天）：
- **标准模式**（`False`，当前配置）：过期自动弹二维码，零星测试更省心。
- **CDP模式**（`True`）：复用本地Edge登录态，反检测更强，批量采集几十条建议用这个，
  但过期后**不会自动弹码**，需要提前手动打开`browser_data/cdp_dy_user_data_dir`对应的
  Edge窗口登录一次抖音网页版刷新cookie。

**素材agent触发抖音相关工具时可能弹出登录扫码窗口，需要有人在旁边处理**——这是有意的设计
简化：`subprocess.run()`本身会同步阻塞等真人扫码完成，不需要额外的检测/暂停/恢复机制，
见`docs/ISSUES.md`"完整素材agent"相关记录。

详见 `docs/ISSUES.md` 第11a条。
