"""Pure helpers for the program-owned sections in ``final_guide.md``."""
from __future__ import annotations

import re


REFERENCE_SECTION_HEADING = "## 参考来源"
POI_SECTION_HEADING = "## 重点景点深度玩法"

_LEGACY_REFERENCE_SECTION_HEADING = "## 📚 参考来源（程序生成，非模型自行撰写）"
_LEGACY_POI_SECTION_HEADING = "## 🌳 重点景点深度玩法（程序拼接自POI详情文件，非模型自行撰写）"

# Current program output uses the canonical headings above. The exact legacy
# reference headings remain reserved so model-written reference tables can be
# removed without matching arbitrary prose headings that contain the same words.
_REFERENCE_HEADING_RE = re.compile(
    rf"(?m)^(?:{re.escape(_LEGACY_REFERENCE_SECTION_HEADING)}|#{{1,6}}[ \t]+(?:📚[ \t]*)?参考来源)[ \t]*$"
)
_POI_HEADING_RE = re.compile(
    rf"(?m)^(?:{re.escape(POI_SECTION_HEADING)}|{re.escape(_LEGACY_POI_SECTION_HEADING)})[ \t]*$"
)
_TRAILING_SEPARATOR_RE = re.compile(r"(?:\n[ \t]*)+---[ \t]*$")


def _heading_start(pattern: re.Pattern[str], content_md: str) -> int | None:
    match = pattern.search(content_md)
    return match.start() if match else None


def _trim_before_section(value: str) -> str:
    value = value.rstrip()
    return _TRAILING_SEPARATOR_RE.sub("", value).rstrip()


def split_final_guide_sections(content_md: str) -> tuple[str, str, str]:
    """Return ``(body, poi_appendix, reference_appendix)`` from either legacy order."""
    content_md = content_md or ""
    reference_start = _heading_start(_REFERENCE_HEADING_RE, content_md)
    poi_start = _heading_start(_POI_HEADING_RE, content_md)

    if reference_start is None and poi_start is None:
        return content_md.strip(), "", ""
    if poi_start is not None and (reference_start is None or poi_start < reference_start):
        body = _trim_before_section(content_md[:poi_start])
        if reference_start is None:
            return body, content_md[poi_start:].strip(), ""
        poi = _trim_before_section(content_md[poi_start:reference_start])
        return body, poi, content_md[reference_start:].strip()

    body = _trim_before_section(content_md[:reference_start])
    if poi_start is None:
        return body, "", content_md[reference_start:].strip()
    references = _trim_before_section(content_md[reference_start:poi_start])
    return body, content_md[poi_start:].strip(), references


def compose_final_guide_sections(body: str, poi_appendix: str = "", reference_appendix: str = "") -> str:
    """Compose the stable order: validated body, POI appendix, references last."""
    sections = [section.strip() for section in (body, poi_appendix, reference_appendix) if section.strip()]
    return "\n\n---\n\n".join(sections) + ("\n" if sections else "")


def contract_body_markdown(content_md: str) -> str:
    """Return only the Integration-authored body governed by the delivery contract."""
    body, _poi_appendix, _reference_appendix = split_final_guide_sections(content_md)
    return body


def normalize_final_guide_section_order(content_md: str) -> str:
    if not _REFERENCE_HEADING_RE.search(content_md) and not _POI_HEADING_RE.search(content_md):
        return content_md
    body, poi_appendix, reference_appendix = split_final_guide_sections(content_md)
    return compose_final_guide_sections(body, poi_appendix, reference_appendix)
