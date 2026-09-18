"""模型运行时配置层（机制C，见docs/ARCHITECTURE.md第13.2节）。

`src/config.py`的`LLM_MODEL`/`LLM_API_KEYS`是进程启动时从`.env`读入就固化的模块常量，
"运行时切换模型、不重启生效"要求一个可变的运行时配置层：这里用一个线程安全的可变单例
保存"当前生效的模型名覆盖值"，`src/llm_rotation.py`的`build_chat_model()`在调用方没有
显式指定model时，优先读这个覆盖值，没设置则回退到该分组的默认值（LLM_MODEL/LLM_LIGHT_MODEL）。

模型名覆盖范围（full/light，见docs/ARCHITECTURE.md第13.2节）：full是核心素材
Agent、部分核验和显式兼容调用的通用分组，light是需求字段抽取/陪伴意图分类/
飞书意图分类等轻量调用的分组。当前长攻略与结构化核验主链由
`TRAVEL_MODEL_PROVIDER`决定，不受 full/light 覆盖影响。两组的模型覆盖值完全独立，
不共用同一份状态。**这个覆盖范围刻意不包含mimo**
（mimo组只有独立的key池，没有模型名运行时切换——用户这次的诉求是key CRUD，不是扩大
模型切换范围，维持原有决策不变，见`src/llm_rotation.py`的`build_chat_model`文档字符串）。

已知限制（已跟用户确认，不为了解决它增加复杂度）：这个运行时配置只在当前进程内生效——
`run_mvp.py`等独立CLI进程是分开的进程，感知不到后台API切换的新配置。CLI本身定位是过渡
方案（飞书接入前的临时入口），长期会被替代，不值得为了跨进程一致性引入外部配置中心。

**2026-08-01更新**：`get_key_status()`不再只读展示——key本身的增删改已经推翻旧政策
移到`src.api.key_store`模块（管理后台"模型/Key"页面支持网页表单直接增删key），
这里只负责把该分组的key池状态（数量/脱敏展示/当前轮询到第几个）组装成前端需要的
形状，实际的key池读写委托给`key_store`。覆盖full/light/mimo/cunai/tavily/poster，
不再只是full/light两组。
"""
from __future__ import annotations

import threading

_lock = threading.Lock()
_model_overrides: dict[str, str | None] = {"full": None, "light": None}


def get_model_override(group: str = "full") -> str | None:
    with _lock:
        return _model_overrides.get(group)


def set_model_override(model: str | None, group: str = "full") -> None:
    """model为None时清除该分组的覆盖值，回退到.env里对应的默认模型。"""
    with _lock:
        _model_overrides[group] = model or None


def get_effective_model(group: str = "full") -> str:
    """build_chat_model()实际使用的模型名：该分组有运行时覆盖值就用它，否则用.env默认值
    （full对应LLM_MODEL，light对应LLM_LIGHT_MODEL）。"""
    from src.config import LLM_LIGHT_MODEL, LLM_MODEL

    override = get_model_override(group)
    default_model = LLM_LIGHT_MODEL if group == "light" else LLM_MODEL
    return override or default_model


def _mask_key(key: str) -> str:
    """委托给key_store._mask_key（单一实现，避免两处脱敏逻辑分叉）——保留这个
    模块级函数名是为了不用改动model_config.py现有的`from src.api.runtime_config
    import _mask_key`调用点。"""
    from src.api.key_store import _mask_key as _key_store_mask_key

    return _key_store_mask_key(key)


def get_key_status(group: str = "full") -> dict:
    """该分组配置了几个key、脱敏后的样子、当前轮询到第几个——不暴露明文。key本身的
    读取委托给`src.api.key_store`（真正的数据来源），这里只负责组装展示形状+判断
    "当前轮询指针"，因为不同分组的"轮询指针"语义不一样：
    - full/light/mimo/cunai：走`src.llm_rotation._current_key_index`。
    - tavily：走`src.tools.web_search_tools._current_key_index`（独立于llm_rotation
      的另一套轮询计数器，因为tavily的请求逻辑是普通requests.post，不走
      RotatingChatOpenAI那套机制）。
    - poster：没有轮询概念（`run_generate_poster.py`固定用列表第一个key，见
      `_current_poster_key()`的文档字符串），`current_key_index`固定为0（有key时）
      或`None`（没有key时），用来告诉前端"这个就是实际会被使用的key"。
    """
    from src.api import key_store

    key_list = key_store.list_keys(group)

    if group in ("full", "light", "mimo", "cunai"):
        from src.llm_rotation import _current_key_index

        current_index = (_current_key_index.get(group, 0) % len(key_list)) if key_list else None
    elif group == "tavily":
        from src.tools.web_search_tools import _current_key_index as tavily_index

        current_index = (tavily_index % len(key_list)) if key_list else None
    else:  # poster
        current_index = 0 if key_list else None

    keys = [
        {"index": i, "masked": _mask_key(k), "is_current": i == current_index}
        for i, k in enumerate(key_list)
    ] if key_list else []
    return {
        "key_count": len(key_list),
        "current_key_index": current_index,
        "keys": keys,
    }
