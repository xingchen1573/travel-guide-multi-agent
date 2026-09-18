"""生成"带原始依据"的溯源版攻略：final_guide.md 保持不变（给用户看的干净版），
另外拼一份 final_guide.annotated.md，把每条建议实际引用的chunk原文附在后面，方便核查
"这条建议是不是真的有依据、还是LLM编的"。纯程序处理，不调用LLM，成本几乎为0。
"""
from __future__ import annotations

import json
from pathlib import Path

from src.config import MATERIAL_LIBRARY_DIR
from src.workspace import store


def _load_chunks_by_id(topic: str) -> dict[str, dict]:
    chunks_path = MATERIAL_LIBRARY_DIR / "index" / topic / "chunks.jsonl"
    if not chunks_path.exists():
        return {}
    result = {}
    for line in chunks_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            c = json.loads(line)
            result[c["chunk_id"]] = c
    return result


def _collect_sources(project_id: str) -> list[dict]:
    """从 lifestyle output 里把每条建议附带的 sources（含chunk_id）都收集出来，
    保留是哪个字段/哪一天引用的，方便annotated版本里分组展示。"""
    lifestyle = store.read_workspace(project_id, "outputs/lifestyle.json") or {}
    collected = []
    for field in ["food", "outfit", "lodging", "play"]:
        for item in lifestyle.get(field, {}).get("items", []):
            for src in item.get("sources", []):
                if src.get("chunk_id"):
                    collected.append(
                        {
                            "cited_in": f"{field}.day{item.get('day')}",
                            "recommendation_preview": item.get("recommendation", "")[:60],
                            **src,
                        }
                    )
    return collected


def annotate_final_guide(project_id: str, topic: str) -> Path:
    final_guide_md = store.read_workspace(project_id, "final_guide.md") or ""
    sources = _collect_sources(project_id)
    chunks_by_id = _load_chunks_by_id(topic)

    lines = [final_guide_md, "\n\n---\n\n## 🔍 溯源附录（程序自动生成，非最终稿正文）\n"]
    lines.append("以下按引用位置列出每条建议实际参考的原始素材片段，用于核实内容是否有依据。\n")

    if not sources:
        lines.append("\n> 本次生成的建议中没有记录到具体chunk_id级别的来源（可能是历史数据或素材库暂无匹配片段）。\n")
    else:
        seen = set()
        for src in sources:
            key = (src["cited_in"], src["chunk_id"])
            if key in seen:
                continue
            seen.add(key)
            chunk = chunks_by_id.get(src["chunk_id"])
            lines.append(f"\n### {src['cited_in']} · {src['recommendation_preview']}...")
            lines.append(f"- 来源：{src.get('platform', '')} / {src.get('author', '') or '未知作者'} / `{src['doc_id']}` / `{src['chunk_id']}`")
            if chunk:
                lines.append(f"- 原文片段：\n\n> {chunk['text']}\n")
            else:
                lines.append("- ⚠️ 原文片段未找到（chunk_id在chunks.jsonl里查不到，可能素材库有变动）\n")

    annotated_content = "\n".join(lines)
    path = store.workspace_dir(project_id) / "final_guide.annotated.md"
    path.write_text(annotated_content, encoding="utf-8")
    return path


if __name__ == "__main__":
    import sys

    project_id = sys.argv[1] if len(sys.argv) > 1 else "chengdu-3d-e2e-v2"
    topic = sys.argv[2] if len(sys.argv) > 2 else "成都"
    path = annotate_final_guide(project_id, topic)
    print(f"已生成溯源版: {path}")
