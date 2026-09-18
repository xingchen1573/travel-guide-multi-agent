from src.workspace.final_guide_sections import (
    POI_SECTION_HEADING,
    REFERENCE_SECTION_HEADING,
    contract_body_markdown,
    normalize_final_guide_section_order,
    split_final_guide_sections,
)


def test_program_owned_headings_are_user_facing():
    assert "程序" not in REFERENCE_SECTION_HEADING
    assert "模型" not in REFERENCE_SECTION_HEADING
    assert "程序" not in POI_SECTION_HEADING
    assert "模型" not in POI_SECTION_HEADING


def test_normalize_legacy_reference_then_poi_order():
    legacy = (
        "# 攻略\n\n## Day 1\n正文\n\n---\n\n"
        f"{REFERENCE_SECTION_HEADING}\n来源A\n\n---\n\n"
        f"{POI_SECTION_HEADING}\n### Day2 青城山\n路线"
    )

    normalized = normalize_final_guide_section_order(legacy)

    assert normalized.index("重点景点深度玩法") < normalized.index("参考来源")
    assert normalized.count("\n\n---\n\n") == 2
    assert contract_body_markdown(normalized) == "# 攻略\n\n## Day 1\n正文"


def test_split_current_order_is_stable():
    current = (
        "# 攻略\n\n## Day 1\n正文\n\n---\n\n"
        f"{POI_SECTION_HEADING}\nPOI\n\n---\n\n"
        f"{REFERENCE_SECTION_HEADING}\n来源"
    )

    body, poi, references = split_final_guide_sections(current)

    assert body.endswith("正文")
    assert poi.startswith(POI_SECTION_HEADING)
    assert references.startswith(REFERENCE_SECTION_HEADING)
    assert normalize_final_guide_section_order(current) == current + "\n"


def test_similar_headings_remain_in_contract_body():
    content = (
        "# 攻略\n\n## Day 1\n正文\n\n"
        "## 参考来源与核验说明\n这是正文中的说明\n\n"
        "## 重点景点深度玩法建议\n这也是正文\n\n"
        "## Day 2\n后续正文"
    )

    body, poi, references = split_final_guide_sections(content)

    assert body == content
    assert poi == ""
    assert references == ""


def test_exact_legacy_reference_heading_is_still_reserved():
    content = "# 攻略\n\n## Day 1\n正文\n\n## 参考来源\n模型自写引用"

    body, poi, references = split_final_guide_sections(content)

    assert body.endswith("正文")
    assert poi == ""
    assert references.startswith("## 参考来源")
