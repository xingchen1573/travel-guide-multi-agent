"""素材缺口检测：判定"检索不到相关内容"是不是真的缺，还是判断方法本身有问题。

对应 docs/ARCHITECTURE.md 第16节 / docs/ISSUES.md 第42条已实现的"POI详情agent定向补充
检索"半自动机制的扩展设计——这次把"判定真缺"这一步沉淀成三条路径（POI详情/route/
lifestyle）共享的通用函数，不再各自维护一份判断逻辑。

**核心教训（直接决定了这里为什么不是简单的"分数 >= 阈值"）**：
docs/ISSUES.md第42条记录过一次误判——简单grep统计"灌县古镇"精确字符串只出现1次，
据此判断"素材不足"，后来发现是ASR把"灌县"同音字错写成了"冠县"，内容其实完整存在。
本次实现前，为了给`score_threshold`找一个有依据的数值，对成都素材库做了一轮真实检索
校准（不是拍脑袋），发现比grep更隐蔽的新坑：**retrieve_materials返回的原始分数
（embedding余弦相似度、BM25分数）本身也无法可靠区分"真的相关"和"只是共享'一日游'/
'攻略'/'讲解'这类通用词"**——真实测试过几个成都素材库里完全不存在的话题
（"西安兵马俑""哈尔滨冰雪大世界""梦幻山谷神秘瀑布"这类虚构地点），因为共享通用词，
combined分数（embedding与BM25归一化后取较大者）达到0.65~0.74，比"灌县古镇"这个
真实存在但需要同音字消解的案例（0.58）还要高——纯数字阈值在这两类情况之间完全没有
可靠的分界线，这跟docs/ISSUES.md第9/21条、CLAUDE.md已知问题第4条反复强调的教训
（"不要相信打分本身，要去验证内容"）是同一类问题，只是这次发生在"判断缺不缺"而不是
"核验幻觉"这个环节。

**因此这里的最终判定标准不是分数，是内容**：score_threshold只做一次轻量初筛（过滤掉
明显不沾边、没必要花一次可能触发LLM调用的字面/别名核对的候选），真正决定"命中/未命中"
的是复用`citation_check._verify_items`（entity_identity_only=True）对候选chunk原文做的
字面核对+别名/同音字消解两档核对——这跟`route_agent`"只关心地名本身有没有依据"是同一个
判断粒度，也是这个项目里唯一已经在真实案例上验证过对"同一实体、不同写法"这类问题有效的
机制，不需要另起一套。
"""
from __future__ import annotations

from pathlib import Path

from src.materials import citation_check
from src.materials.retrieve import retrieve_materials

# 一小撮"泛化后缀词"：query_seed按约定是"实体 + 泛化角度词"这种空格分隔的短语
# （比如"武侯祠 讲解"、"都江堰 灌县古镇 攻略"），这些词本身不是要核对的具体实体，
# 核对时要先剔除，避免把"讲解"当成一个claimed_entity去做字面匹配（毫无意义，
# 也会拖累_verify_items的判断）。这份词表由调用方（三条触发路径）的query_seed
# 生成规则决定，不需要穷尽，只要覆盖已知会用到的角度词即可。
_GENERIC_QUERY_WORDS = frozenset(
    {
        "讲解", "攻略", "介绍", "推荐", "详细", "内容", "信息",
        "情况", "流程", "顺序", "具体", "怎么样", "怎么走",
        "美食", "穿搭", "住宿", "游玩",
    }
)

# BM25原始分数没有统一量纲（依赖语料规模/词频分布），这里粗略归一化到跟embedding
# 余弦相似度（0~1）同一个数量级，只是为了让score_threshold能同时套用在两路信号上，
# 不追求精确换算——本次校准观察到这个项目当前素材规模下，真正相关的检索结果bm25分数
# 常见落在8~19区间，15是这个区间里偏保守（不会让明显更低分的候选被误判为"达标"）的
# 一个粗略参考值，具体见docs/ISSUES.md新增记录。
_BM25_NORMALIZATION_CAP = 15.0


def _normalize_bm25(bm25_score: float | None) -> float:
    if bm25_score is None:
        return 0.0
    return min(bm25_score / _BM25_NORMALIZATION_CAP, 1.0)


def _query_core_entities(seed: str) -> list[str]:
    """从query_seed（约定是空格分隔的"实体 [+ 泛化角度词...]"短语）里剔除泛化词，
    剩下的词才是真正要核对"有没有依据"的实体。全部词都是泛化词时（理论上不应该发生，
    调用方的生成规则总该留一个具体实体）兜底返回全部词，不返回空列表——空
    claimed_entities会被_verify_items判成"完全没有引用依据"的问题分支，语义不对。
    """
    words = [w for w in seed.split() if w.strip()]
    core = [w for w in words if w not in _GENERIC_QUERY_WORDS]
    return core or words


def check_material_gap(
    topic: str,
    query_seeds: list[str],
    score_threshold: float = 0.3,
    top_k: int = 5,
    *,
    additional_library_dirs: list[Path] | None = None,
) -> bool:
    """真的调用检索验证"是不是缺"，不是靠grep关键词计数（docs/ISSUES.md第42条教训）。

    对query_seeds里每一个问法都跑一次retrieve_materials，取combined分数
    （max(embedding余弦相似度, 归一化后的bm25分数)）达到score_threshold的候选，
    复用citation_check._verify_items（entity_identity_only=True，跟route_agent的
    核验粒度一致——只关心这个实体本身有没有依据，不关心描述细节）核对这个问法的核心
    实体是不是真的在候选chunk原文里有依据（含常见地标后缀剥离、别名/同音字消解）。

    任意一个问法命中（_verify_items判定无问题），立即返回False（不是真缺）。
    全部问法都没有命中，才返回True（真缺）。

    没有素材库/检索结果完全为空（比如topic对应的index还不存在）时，每个问法都会
    因为candidates为空而直接跳过核对，最终返回True——这也是合理语义："完全没有可用
    候选"当然算"缺"，不需要特殊处理。
    """
    for seed in query_seeds:
        results = retrieve_materials(topic, seed, top_k=top_k)
        for library_dir in additional_library_dirs or []:
            results.extend(
                retrieve_materials(
                    topic,
                    seed,
                    top_k=top_k,
                    library_dir=library_dir,
                )
            )
        candidates = {
            r["chunk_id"]: r["content_snippet"]
            for r in results
            if max(
                r["score"].get("embedding") or 0.0,
                _normalize_bm25(r["score"].get("bm25")),
            )
            >= score_threshold
        }
        if not candidates:
            continue

        core_entities = _query_core_entities(seed)
        issues = citation_check._verify_items(
            [
                {
                    "label": seed,
                    "claimed_entities": core_entities,
                    "chunk_ids": list(candidates.keys()),
                    "content": seed,
                }
            ],
            candidates,
            entity_identity_only=True,
        )
        if not issues:
            return False  # 这个问法命中了，不是真缺

    return True
