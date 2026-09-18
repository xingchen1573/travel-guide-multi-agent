"""模型/Key管理接口：当前长攻略 Provider、full/light/mimo/cunai 兼容
分组与搜索/生图 key 池。
LLM对话分组的模型名/Key池状态展示 + 运行时按分组切换模型（只full/light支持，不重启
API进程生效）；tavily/poster两个key池；Amap x3/Embedding这几个仍然`.env`-only的
服务只读展示。

`primary_travel` 是展示当前生产路由的权威字段；下方四个 groups 为 Key 管理/
兼容调用分组，不再把历史分组名当成长攻略主链。

**2026-08-01推翻旧政策**：此前"Key本身不做新增/编辑的写接口，只能走.env文件本身"
这条限制已被用户明确推翻——`/grilling`访谈定案，full/light/mimo/cunai/tavily/poster
共5个分组现在都支持网页表单直接增删key（`POST/DELETE /{group}/keys`），实时生效
不需要重启进程，实际读写委托给`src.api.key_store`。高德/Embedding/飞书这几类
仍然保持`.env`-only只读，用户明确排除在这次CRUD范围外。

2026-07-26新增：POST /test 手动可用性检测——此前页面只展示"配置了什么"，从没有真正
发起过一次调用去验证"这个key/模型现在真的能用"。手动触发（不做自动/定时检查，真实
调用有真实token成本），支持测试单个key或该分组全部key。mimo/tavily走各自的真实
测试逻辑；poster出于生图按次收费的真实成本考虑，不做真实生成测试。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from src.api import key_store, runtime_config

router = APIRouter(prefix="/api/model_config", tags=["model_config"])

_GROUP_LABELS = {
    "full": "通用 full 渠道（素材 Agent 与兼容调用）",
    "light": "分类/抽取（需求收集、陪伴意图路由等轻量调用）",
    "mimo": "陪伴问答兼容渠道（历史 mimo 分组）",
    "cunai": "CunAI 直连兼容渠道",
}

_GROUP_USAGES = {
    "full": "核心素材 Agent、部分引用/路线检查及显式 full 兼容调用；不控制当前长攻略主链。",
    "light": "主 Agent 需求抽取、确认/意图分类等轻量调用；不参与核心攻略内容生成。",
    "mimo": "陪伴问答主用（full 兜底），也可在定向复核中显式选择；非当前长攻略主链。Key 池兼容 gptload/token_plan/legacy 渠道。",
    "cunai": "定向复核可选兼容渠道；默认复核与长攻略都不使用它，非当前长攻略主链。",
}

_KEY_POOL_LABELS = {
    "tavily": ("Tavily 搜索", "realtime_agent联网查天气/营业状态等时效性信息，支持多key轮询"),
    "poster": ("生图", "run_generate_poster.py一次性生成概览海报，经cun.ai中转，不做自动轮询"),
}

_VALID_KEY_GROUPS = ("full", "light", "mimo", "cunai", "tavily", "poster")


def _group_status(group: str, default_model: str) -> dict:
    return {
        "group": group,
        "label": _GROUP_LABELS[group],
        "usage": _GROUP_USAGES[group],
        "default_model": default_model,
        "effective_model": runtime_config.get_effective_model(group),
        "override_active": runtime_config.get_model_override(group) is not None,
        **runtime_config.get_key_status(group),
    }


def _mimo_group_status() -> dict:
    """mimo分组没有模型名运行时切换（只有key池CRUD），跟full/light的_group_status
    形状不同——不含effective_model/override_active这两个"模型名覆盖"专属字段。"""
    from src.config import MIMO_CHAT_MODEL, MIMO_PRO_CHAT_MODEL

    return {
        "group": "mimo",
        "label": _GROUP_LABELS["mimo"],
        "usage": _GROUP_USAGES["mimo"],
        "default_model": MIMO_CHAT_MODEL,
        "available_models": list(dict.fromkeys((MIMO_CHAT_MODEL, MIMO_PRO_CHAT_MODEL))),
        **runtime_config.get_key_status("mimo"),
    }


def _cunai_group_status() -> dict:
    from src.config import CUNAI_CHAT_MODEL

    return {
        "group": "cunai",
        "label": _GROUP_LABELS["cunai"],
        "usage": _GROUP_USAGES["cunai"],
        "default_model": CUNAI_CHAT_MODEL,
        "available_models": [CUNAI_CHAT_MODEL],
        **runtime_config.get_key_status("cunai"),
    }


def _primary_travel_status() -> dict:
    """返回当前进程真正的长攻略/结构化核验路由。

    full/mimo/cunai 是兼容分组和 Key 管理入口，不能由它们的历史标签
    反推生产主链；主链唯一取决于 TRAVEL_MODEL_PROVIDER。
    """
    from src.config import (
        GPTLOAD_BASE_URL,
        GPTLOAD_CHAT_MODEL,
        JIJI_API_KEYS,
        JIJI_BASE_URL,
        JIJI_CHAT_MODEL,
        TRAVEL_MODEL_PROVIDER,
    )

    if TRAVEL_MODEL_PROVIDER == "jiji":
        provider = "jiji.cc"
        base_url = JIJI_BASE_URL
        model = JIJI_CHAT_MODEL
        key_count = len(JIJI_API_KEYS)
    else:
        provider = "GPT-Load"
        base_url = GPTLOAD_BASE_URL
        model = GPTLOAD_CHAT_MODEL
        key_count = sum(
            entry.get("channel") == "gptload"
            for entry in key_store.list_mimo_keys()
        )

    return {
        "label": "当前长攻略与结构化核验主链",
        "provider_group": TRAVEL_MODEL_PROVIDER,
        "provider": provider,
        "base_url": base_url,
        "model": model,
        "usage": "路线、衣食住行、避雷、POI详情、文化、最终整合及结构化核验",
        "configured": key_count > 0,
        "key_count": key_count,
    }


def _key_pool_status(group: str) -> dict:
    """tavily/poster这类"纯key池，没有模型名概念"的分组状态——不含
    default_model/effective_model/override_active，只有key池本身的信息。"""
    label, usage = _KEY_POOL_LABELS[group]
    return {
        "group": group,
        "label": label,
        "usage": usage,
        **runtime_config.get_key_status(group),
    }


def _other_services() -> list[dict]:
    """仍然`.env`-only、只读脱敏展示的几类服务——Tavily/生图已经CRUD化搬去
    key_pools，这里只剩下高德三件套+Embedding这几类用户明确排除在CRUD范围外的
    服务（架构上是"全局共享单一凭证"，不像LLM key那样有429轮询需求）。
    """
    from src.config import (
        AMAP_GEOCODE_API_KEY,
        AMAP_JS_API_KEY,
        AMAP_MCP_API_KEY,
        EMBEDDING_API_KEY,
    )

    def _single(name: str, usage: str, key: str | None) -> dict:
        return {
            "name": name,
            "usage": usage,
            "configured": bool(key),
            "key_count": 1 if key else 0,
            "masked_keys": [runtime_config._mask_key(key)] if key else [],
        }

    return [
        _single("高德MCP", "route_agent路线核验（amap_query工具），未配置时走stub", AMAP_MCP_API_KEY),
        _single("高德地图-服务端地理编码", "森林页真实地图POI经纬度转换", AMAP_GEOCODE_API_KEY),
        _single("高德地图-前端JS API", "森林页生成的静态网站里加载地图组件", AMAP_JS_API_KEY),
        _single("Embedding", "素材库向量检索（独立provider，不一定和对话模型同端点）", EMBEDDING_API_KEY),
    ]


@router.get("")
def get_model_config() -> dict:
    from src.config import LLM_LIGHT_MODEL, LLM_MODEL

    return {
        "primary_travel": _primary_travel_status(),
        "groups": {
            "full": _group_status("full", LLM_MODEL),
            "light": _group_status("light", LLM_LIGHT_MODEL),
            "cunai": _cunai_group_status(),
            "mimo": _mimo_group_status(),
        },
        "key_pools": {
            "tavily": _key_pool_status("tavily"),
            "poster": _key_pool_status("poster"),
        },
        "other_services": _other_services(),
    }


class SetModelRequest(BaseModel):
    model: str | None = None
    group: str = "full"


@router.put("/model")
def set_model(body: SetModelRequest) -> dict:
    """model为None/空字符串时清除该分组的覆盖值，回退到.env默认模型。只支持full/light
    两组（mimo没有模型名运行时切换，见_mimo_group_status文档字符串）。只在当前API进程内
    生效，独立CLI进程（run_mvp.py等）不受影响，这是明确的设计决策，见docs/ARCHITECTURE.md
    第13.2节。
    """
    if body.group not in ("full", "light"):
        raise HTTPException(status_code=400, detail=f"分组{body.group!r}不支持模型名切换（只支持full/light）")
    runtime_config.set_model_override(body.model, body.group)
    return {"group": body.group, "effective_model": runtime_config.get_effective_model(body.group)}


class AddKeyRequest(BaseModel):
    key: str
    channel: str | None = None  # 仅mimo必填：gptload/token_plan/legacy


@router.post("/{group}/keys", status_code=201)
def add_key(group: str, body: AddKeyRequest) -> dict:
    """追加一个key到该分组末尾，实时生效（不需要重启API进程）。"""
    if group not in _VALID_KEY_GROUPS:
        raise HTTPException(status_code=400, detail=f"未知分组 {group!r}，支持的分组：{_VALID_KEY_GROUPS}")
    try:
        key_store.add_key(group, body.key, channel=body.channel)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return runtime_config.get_key_status(group)


@router.delete("/{group}/keys/{index}")
def delete_key(group: str, index: int) -> dict:
    if group not in _VALID_KEY_GROUPS:
        raise HTTPException(status_code=400, detail=f"未知分组 {group!r}，支持的分组：{_VALID_KEY_GROUPS}")
    try:
        key_store.delete_key(group, index)
    except IndexError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return runtime_config.get_key_status(group)


class TestKeyRequest(BaseModel):
    group: str = "full"
    key_index: int | None = None  # None时测试该分组全部key；否则只测指定下标那一个


def _test_llm_group_keys(group: str, key_index: int | None) -> dict:
    """LLM分组共用的真实最小成本调用测试。mimo组的key跟
    full/light不同——每个key可能对应不同的base_url（token_plan渠道 vs 旧渠道），
    不能像full/light那样共用一个base_url，需要逐key读取。"""
    import time

    from langchain_openai import ChatOpenAI

    from src.api.key_store import _mask_key
    from src.config import (
        CUNAI_BASE_URL,
        CUNAI_CHAT_MODEL,
        LLM_BASE_URL,
        MIMO_CHAT_BASE_URL,
        MIMO_CHAT_MODEL,
        MIMO_GPTLOAD_BASE_URL,
        MIMO_TOKEN_PLAN_BASE_URL,
    )

    if group == "mimo":
        entries = key_store.list_mimo_keys()
        key_list = [e["key"] for e in entries]
        base_urls = [
            MIMO_GPTLOAD_BASE_URL
            if e["channel"] == "gptload"
            else MIMO_TOKEN_PLAN_BASE_URL
            if e["channel"] == "token_plan"
            else MIMO_CHAT_BASE_URL
            for e in entries
        ]
        model_name = MIMO_CHAT_MODEL
    elif group == "cunai":
        key_list = key_store.list_keys(group)
        base_urls = [CUNAI_BASE_URL] * len(key_list)
        model_name = CUNAI_CHAT_MODEL
    else:
        key_list = key_store.list_keys(group)
        base_urls = [LLM_BASE_URL] * len(key_list)
        model_name = runtime_config.get_effective_model(group)

    indices = [key_index] if key_index is not None else list(range(len(key_list)))

    results = []
    for i in indices:
        if i < 0 or i >= len(key_list):
            continue
        key = key_list[i]
        base_url = base_urls[i]
        start = time.monotonic()
        try:
            model = ChatOpenAI(base_url=base_url, api_key=key, model=model_name, temperature=0, timeout=15)
            resp = model.invoke("请只回复两个字：可用")
            latency_ms = round((time.monotonic() - start) * 1000)
            content = resp.content if isinstance(resp.content, str) else str(resp.content)
            results.append({
                "index": i, "masked": _mask_key(key), "ok": True,
                "latency_ms": latency_ms, "error": None, "response_snippet": content[:30],
            })
        except Exception as exc:  # noqa: BLE001 — 测试端点本身不能因为某个key异常而整体500
            latency_ms = round((time.monotonic() - start) * 1000)
            results.append({
                "index": i, "masked": _mask_key(key), "ok": False,
                "latency_ms": latency_ms, "error": str(exc)[:300], "response_snippet": None,
            })

    return {"group": group, "model": model_name, "results": results}


def _test_tavily_keys(key_index: int | None) -> dict:
    """对每个待测key发起一次真实的最小Tavily搜索，成本极低（不像LLM调用有token
    成本，Tavily免费层足够覆盖手动测试），能直接证明这个key端到端可用。"""
    import time

    import requests

    from src.api.key_store import _mask_key

    key_list = key_store.list_keys("tavily")
    indices = [key_index] if key_index is not None else list(range(len(key_list)))

    results = []
    for i in indices:
        if i < 0 or i >= len(key_list):
            continue
        key = key_list[i]
        start = time.monotonic()
        try:
            resp = requests.post(
                "https://api.tavily.com/search",
                json={"api_key": key, "query": "test", "search_depth": "basic", "max_results": 1},
                timeout=15,
            )
            latency_ms = round((time.monotonic() - start) * 1000)
            if resp.ok:
                results.append({
                    "index": i, "masked": _mask_key(key), "ok": True,
                    "latency_ms": latency_ms, "error": None, "response_snippet": None,
                })
            else:
                results.append({
                    "index": i, "masked": _mask_key(key), "ok": False,
                    "latency_ms": latency_ms, "error": f"HTTP {resp.status_code}", "response_snippet": None,
                })
        except Exception as exc:  # noqa: BLE001
            latency_ms = round((time.monotonic() - start) * 1000)
            results.append({
                "index": i, "masked": _mask_key(key), "ok": False,
                "latency_ms": latency_ms, "error": str(exc)[:300], "response_snippet": None,
            })

    return {"group": "tavily", "model": None, "results": results}


def _test_poster_keys() -> dict:
    """生图是按次收费的真实API调用，不做自动测试——`run_generate_poster.py`本身的
    文档字符串也强调这一点。这里只做"key是否存在"的展示，明确告知未做真实验证，
    避免未来有人"顺手"把这里改成真的发起生成请求。"""
    from src.api.key_store import _mask_key

    key_list = key_store.list_keys("poster")
    results = [
        {
            "index": i, "masked": _mask_key(k), "ok": None,
            "latency_ms": None,
            "error": "生图为按次收费真实调用，出于成本考虑不做自动测试，请直接尝试生成一次海报来验证",
            "response_snippet": None,
        }
        for i, k in enumerate(key_list)
    ]
    return {"group": "poster", "model": None, "results": results}


@router.post("/test")
def test_keys(body: TestKeyRequest) -> dict:
    """手动触发一次验证调用，确认指定key（或该分组全部key）现在是否真的可用。
    full/light/mimo/cunai走真实最小成本LLM调用；tavily走真实最小成本搜索；poster出于
    真实计费成本考虑不发起真实生成，只展示"未测试"提示。

    故意不用RotatingChatOpenAI（会在429时自动换到下一个key重试，那样测出来的"成功"
    可能其实是另一个key的功劳，掩盖了被测key本身已经失效这件事）——这里用普通ChatOpenAI
    显式绑定单个key，一个key对应一次独立请求，结果互不掩盖。
    """
    if body.group in ("full", "light", "mimo", "cunai"):
        return _test_llm_group_keys(body.group, body.key_index)
    if body.group == "tavily":
        return _test_tavily_keys(body.key_index)
    if body.group == "poster":
        return _test_poster_keys()
    raise HTTPException(status_code=400, detail=f"未知分组 {body.group!r}")
