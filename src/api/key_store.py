"""Key池运行时配置层：full/light/mimo/cunai（LLM对话）+tavily（搜索）+poster
（生图）等分组的 key 池。现行分组支持网页表单直接增删改，
实时生效不需要重启进程（管理后台"模型/Key"页面第二轮 CRUD化，2026-08-01，
`/grilling`访谈定案）。

这条推翻了此前"不在网页表单里输入明文密钥"的政策——用户明确要求"推翻旧策略，支持
网页直接增删改key"。高德/embedding/飞书这几类服务的key仍然只能走`.env`，不在这个
模块管辖范围内（用户明确排除，见docs/ARCHITECTURE.md第13.2节）。

存储：`data/runtime_config/api_keys.json`，明文JSON（跟现有`.env`本身也是明文一致，
只是从"进程启动时读一次"变成"运行时可读可写"），前端展示时脱敏。这个文件不应该被
提交进git（`.gitignore`需要包含`data/runtime_config/`）。

首次访问时如果这个文件还不存在/内容为空，从`src.config`当前的`.env`值自动迁移一次
作为初始key池（迁移后运行时以这里的值为准）。这个迁移只做一次（幂等：文件已经
有内容就不再重新灌入），调用方不需要关心这个细节，每次读写前都会惰性检查一次。
"""
from __future__ import annotations

import json
import threading
from typing import Optional

from src.config import DATA_DIR

KEY_STORE_PATH = DATA_DIR / "runtime_config" / "api_keys.json"

_GROUPS = ("full", "light", "mimo", "cunai", "tavily", "poster")
_MIMO_CHANNELS = ("gptload", "token_plan", "legacy")
_MIMO_CHANNEL_PRIORITY = {channel: index for index, channel in enumerate(_MIMO_CHANNELS)}

_lock = threading.Lock()


def _validate_group(group: str) -> None:
    if group not in _GROUPS:
        raise ValueError(f"未知分组 {group!r}，支持的分组：{_GROUPS}")


def _mask_key(key: str) -> str:
    if len(key) <= 8:
        return "*" * len(key)
    return f"{key[:4]}...{key[-4:]}"


def _read_all_unlocked() -> dict:
    if not KEY_STORE_PATH.exists():
        return {}
    return json.loads(KEY_STORE_PATH.read_text(encoding="utf-8"))


def _write_all_unlocked(data: dict) -> None:
    KEY_STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    KEY_STORE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _seed_from_env() -> dict:
    """从src.config当前值构造初始key池——只在ensure_migrated()判定为"新存储为空"时
    调用一次。full/light/tavily是扁平字符串列表；poster是POSTER_IMAGE_API_KEY单值
    包成单元素列表（未配置时为空列表）；mimo保留渠道字段，按
    gptload、token_plan、legacy排序。标准版和Pro共享同一组entries。"""
    from src.config import (
        CUNAI_API_KEYS,
        LLM_API_KEYS,
        LLM_API_KEYS_LIGHT,
        MIMO_API_KEYS,
        GPTLOAD_API_KEYS,
        MIMO_TOKEN_PLAN_API_KEYS,
        POSTER_IMAGE_API_KEY,
        TAVILY_API_KEYS,
    )

    mimo_entries = [{"key": k, "channel": "gptload"} for k in GPTLOAD_API_KEYS]
    mimo_entries += [{"key": k, "channel": "token_plan"} for k in MIMO_TOKEN_PLAN_API_KEYS]
    mimo_entries += [{"key": k, "channel": "legacy"} for k in MIMO_API_KEYS]

    return {
        "full": list(LLM_API_KEYS),
        "light": list(LLM_API_KEYS_LIGHT),
        "mimo": mimo_entries,
        "cunai": list(CUNAI_API_KEYS),
        "tavily": list(TAVILY_API_KEYS),
        "poster": [POSTER_IMAGE_API_KEY] if POSTER_IMAGE_API_KEY else [],
    }


def ensure_migrated() -> None:
    """幂等迁移 + 分组对齐：
    1. 新存储为空（文件不存在或内容是`{}`）时，从当前`.env`值灌入完整初始key池；
    2. 存储已有内容时，补齐缺失的现行分组，并删除已下线的历史分组；已有
       现行分组的 key 列表绝不覆盖。

    惰性调用（每次公开读写函数入口调用一次），不在模块导入时做文件IO副作用。
    """
    with _lock:
        data = _read_all_unlocked()
        if not data:
            _write_all_unlocked(_seed_from_env())
            return
        seed = _seed_from_env()
        changed = False
        for group in list(data):
            if group not in _GROUPS:
                del data[group]
                changed = True
        for group in _GROUPS:
            if group not in data:
                data[group] = seed.get(group, [])
                changed = True
        if changed:
            _write_all_unlocked(data)


def list_keys(group: str) -> list[str]:
    """返回该分组的明文key列表。mimo组会被拍平成裸key字符串列表（丢弃channel信息，
    这是给"只关心key本身、不关心渠道"的调用方（如轮询计数/脱敏展示）用的——需要
    channel信息的调用方（如构造base_url）应该用list_mimo_keys()。"""
    _validate_group(group)
    ensure_migrated()
    with _lock:
        data = _read_all_unlocked()
        entries = data.get(group, [])
        if group == "mimo":
            return [e["key"] for e in entries]
        return list(entries)


def list_mimo_keys() -> list[dict]:
    """mimo组专用：返回带gptload/token_plan/legacy channel的完整结构，
    供src.llm_rotation._mimo_keys_and_urls()按channel决定每个key配对哪个base_url。"""
    ensure_migrated()
    with _lock:
        data = _read_all_unlocked()
        return list(data.get("mimo", []))


def add_key(group: str, key: str, channel: Optional[str] = None) -> None:
    """追加一个key到该分组末尾。mimo组必须传合法channel，
    其余分组忽略channel参数。"""
    _validate_group(group)
    if not key or not key.strip():
        raise ValueError("key不能为空")
    if group == "mimo":
        if channel not in _MIMO_CHANNELS:
            raise ValueError(f"mimo组的channel必须是{_MIMO_CHANNELS}之一，收到{channel!r}")
    ensure_migrated()
    with _lock:
        data = _read_all_unlocked()
        entries = data.setdefault(group, [])
        if group == "mimo":
            entry = {"key": key.strip(), "channel": channel}
            priority = _MIMO_CHANNEL_PRIORITY[channel]
            insert_at = next(
                (
                    index
                    for index, existing in enumerate(entries)
                    if _MIMO_CHANNEL_PRIORITY.get(existing.get("channel"), len(_MIMO_CHANNELS)) > priority
                ),
                len(entries),
            )
            entries.insert(insert_at, entry)
        else:
            entries.append(key.strip())
        _write_all_unlocked(data)


def delete_key(group: str, index: int) -> None:
    """删除该分组第index个key。index越界时抛IndexError。"""
    _validate_group(group)
    ensure_migrated()
    with _lock:
        data = _read_all_unlocked()
        entries = data.get(group, [])
        if index < 0 or index >= len(entries):
            raise IndexError(f"分组{group!r}没有第{index}个key（当前共{len(entries)}个）")
        entries.pop(index)
        data[group] = entries
        _write_all_unlocked(data)
