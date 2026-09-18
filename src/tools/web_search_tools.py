"""实时agent专用的联网搜索工具（Tavily）。

跟src/tools/amap_tools.py同一种"没有真实key时优雅降级"模式：没有配置key时返回明确的
"未配置"提示，让agent能基于自身知识继续回答（并如实声明信息可能不是最新的），而不是
报错中断整个graph。

支持多key轮询（跟src/llm_rotation.py同一个动机：免费版单key额度有限，遇到429限流
或配额耗尽时换下一个key重试），但不需要llm_rotation.py那套"子类化ChatOpenAI._generate"
的复杂机制——这里只是一个普通的requests.post调用，直接在工具函数内部按key列表顺序
重试即可。

用requests直接调用Tavily的REST API（https://api.tavily.com/search），不引入
tavily-python SDK——项目目前没有这个依赖，一个简单的POST请求不需要为此新增一个包。

**2026-08-01更新**：key不再直接从`src.config`的`TAVILY_API_KEYS`模块常量读取，改为
每次调用时从`src.api.key_store`实时读取——管理后台"模型/Key"页面支持网页表单直接
增删tavily分组的key，这个函数必须每次都读最新值，而不是缓存导入时的旧列表，
否则网页上加/删key不会立即生效。
"""
from __future__ import annotations

import json
import threading
import time

import requests
from langchain_core.tools import tool

from src.config import PROJECTS_DIR
from src.materials.web_evidence import build_web_search_log_entry, render_web_results
from src.telemetry.provider_calls import record_provider_call

_TAVILY_SEARCH_URL = "https://api.tavily.com/search"

_lock = threading.Lock()
_current_key_index = 0


def _current_keys() -> list[str]:
    from src.api import key_store

    return key_store.list_keys("tavily")


def _advance_key() -> None:
    global _current_key_index
    with _lock:
        _current_key_index += 1


def _current_key() -> str:
    keys = _current_keys()
    with _lock:
        return keys[_current_key_index % len(keys)]


def _web_search_log_path(project_id: str, log_scope: str):
    return PROJECTS_DIR / project_id / "agents" / log_scope / "web_search_log.jsonl"


def reset_web_search_log(project_id: str, log_scope: str) -> None:
    """清空这个log_scope的web_search证据日志——只应该在"一次生成"真正开始的节点入口调用
    （route_draft_node/lifestyle_agent_node/poi_detail_agent_node/poi_detail_edit_node），
    核验失败后的修复重试节点（route_citation_check_node等）不应该调用这个，否则会冲掉
    最初生成阶段已经查到的web证据（见docs/ISSUES.md第115条方向C）。"""
    _web_search_log_path(project_id, log_scope).unlink(missing_ok=True)


def _append_web_search_log(
    project_id: str, log_scope: str, query: str, results: list[dict]
) -> None:
    path = _web_search_log_path(project_id, log_scope)
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = build_web_search_log_entry(query, results)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def make_web_search_tools(
    project_id: str,
    log_scope: str,
    *,
    research_context: dict | None = None,
) -> list:
    def _record_research(
        *, status: str, query: str, result_count: int = 0, metadata: dict | None = None
    ) -> None:
        if not research_context:
            return
        from src.materials.research_service import record_external_research

        record_external_research(
            project_id,
            actor=str(research_context.get("actor") or "unknown_agent"),
            stage=str(research_context.get("stage") or "unknown"),
            target_key=str(research_context.get("target_key") or log_scope),
            query=query,
            source_kind="web",
            status=status,
            result_count=result_count,
            metadata={"log_scope": log_scope, **(metadata or {})},
        )

    @tool
    def web_search(query: str) -> str:
        """联网搜索真正需要时效性信息的问题（天气、某个店铺是否还在营业、最新的临时闭馆/
        活动信息）。query应该是具体的搜索关键词（如"成都武侯祠 2026年7月 开放时间"），
        不要用整句自然语言提问。返回搜索到的若干条结果摘要，供你阅读后自己判断、
        总结出回答，不要把某一条结果原样当作确定事实直接转述——不同来源可能有分歧
        或已经过时，需要你自己综合判断。"""
        keys = _current_keys()
        if not keys:
            record_provider_call(project_id, provider="web", operation="search", status="unavailable", log_scope=log_scope, metadata={"reason": "key_missing"}, projects_dir=PROJECTS_DIR)
            _record_research(status="unavailable", query=query, metadata={"reason": "key_missing"})
            return (
                "联网搜索未配置（缺少Tavily key），无法查询实时信息。"
                "请基于你自己的知识回答，并如实告知用户这类信息可能不是最新的，建议现场核实。"
            )

        last_error: str | None = None
        for _ in range(len(keys)):
            api_key = _current_key()
            started = time.monotonic()
            try:
                resp = requests.post(
                    _TAVILY_SEARCH_URL,
                    json={
                        "api_key": api_key,
                        "query": query,
                        "search_depth": "basic",
                        "max_results": 5,
                    },
                    timeout=15,
                )
            except requests.RequestException as e:
                last_error = f"请求失败（{e}）"
                record_provider_call(project_id, provider="web", operation="search", status="error", duration_ms=round((time.monotonic() - started) * 1000), log_scope=log_scope, metadata={"error_type": type(e).__name__}, projects_dir=PROJECTS_DIR)
                continue

            # 429=限流，432/433是Tavily额度耗尽/无效key的状态码——都值得换下一个key重试；
            # 其他4xx/5xx大概率是query本身或服务端问题，换key也无济于事，不重试直接报告。
            if resp.status_code in (429, 432, 433):
                last_error = f"key额度耗尽或被限流（HTTP {resp.status_code}）"
                _advance_key()
                record_provider_call(project_id, provider="web", operation="search", status="retryable_error", duration_ms=round((time.monotonic() - started) * 1000), log_scope=log_scope, metadata={"http_status": resp.status_code}, projects_dir=PROJECTS_DIR)
                continue
            if not resp.ok:
                record_provider_call(project_id, provider="web", operation="search", status="error", duration_ms=round((time.monotonic() - started) * 1000), log_scope=log_scope, metadata={"http_status": resp.status_code}, projects_dir=PROJECTS_DIR)
                _record_research(
                    status="error",
                    query=query,
                    metadata={"http_status": resp.status_code},
                )
                return (
                    f"联网搜索请求失败（HTTP {resp.status_code}），请基于你自己的知识回答，"
                    f"并如实告知用户这类信息可能不是最新的，建议现场核实。"
                )

            data = resp.json()
            results = data.get("results") or []
            if not results:
                record_provider_call(project_id, provider="web", operation="search", status="empty", duration_ms=round((time.monotonic() - started) * 1000), log_scope=log_scope, projects_dir=PROJECTS_DIR)
                _record_research(status="empty", query=query)
                return "联网搜索没有找到相关结果，请基于你自己的知识回答，并如实告知用户这类信息可能不是最新的，建议现场核实。"

            result_text = render_web_results(results)
            # 副作用：把真正查到的结果落盘，供citation_check核验platform=="web_verified"
            # 来源时读取（见docs/ISSUES.md第115条方向C）。只记录真正有内容的成功分支，
            # 上面"未配置key"/"无结果"/"请求失败"等降级分支不是证据，不记录。
            _append_web_search_log(project_id, log_scope, query, results)
            record_provider_call(project_id, provider="web", operation="search", status="success", duration_ms=round((time.monotonic() - started) * 1000), log_scope=log_scope, metadata={"result_count": len(results)}, projects_dir=PROJECTS_DIR)
            _record_research(
                status="success",
                query=query,
                result_count=len(results),
                metadata={
                    "retrieved_urls": [
                        str(item.get("url") or "") for item in results if item.get("url")
                    ]
                },
            )
            return result_text

        _record_research(
            status="error",
            query=query,
            metadata={"reason": last_error or "all_keys_unavailable"},
        )
        return (
            f"联网搜索所有已配置的key都不可用（{last_error}），请基于你自己的知识回答，"
            f"并如实告知用户这类信息可能不是最新的，建议现场核实。"
        )

    return [web_search]
