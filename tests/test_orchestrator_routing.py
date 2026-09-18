"""orchestrator.py路由函数的单元测试，重点是_route_integration_phase的"缺一步复核"和
"retry_count随新conflict id重置"这两个bug场景（见docs/ISSUES.md）。不调用真实LLM，
只测纯路由/状态判断逻辑，用isolated_project fixture隔离文件系统（复用test_store.py的模式）。
"""
import pytest

from src import orchestrator
from src.orchestrator import MAX_RETRY, _route_integration_phase
from src.workspace import store


@pytest.fixture
def isolated_project(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path)
    project_id = "test-project"
    store.init_project(project_id)
    return project_id


def test_integration_phase_forces_recheck_before_rebounce(isolated_project):
    """缺一步复核的bug场景：子agent刚修复完（awaiting_integration_recheck=True）时，
    哪怕还有一条pending conflict，也必须先回integration_agent_node复核，不能直接又打回。
    lifestyle_agent_done/pitfall_agent_done都已完成（True），排除下游重置这条独立场景的
    干扰，单独隔离验证"缺一步复核"本身。
    """
    state = {
        "project_id": isolated_project,
        "awaiting_integration_recheck": True,
        "integration_retry_count": 0,
        "lifestyle_agent_done": True,
        "pitfall_agent_done": True,
    }
    # 即使这里存在一条pending conflict，也不该被路由函数看到/处理
    store.report_conflict(isolated_project, actor="integration_agent", target="lifestyle_agent.food", reason="太远了")

    assert _route_integration_phase(state) == "integration_agent_node"


def test_integration_phase_recheck_routes_to_lifestyle_first_when_reset_by_route_bounce(isolated_project):
    """真实排查docs/ISSUES.md第32条已知局限②后发现并修复的场景：route_draft_node被
    target=route_agent的冲突打回重新生成骨架后，会连带把lifestyle_agent_done重置为False
    （骨架可能已经变了，衣食住行的既有推荐需要重新看一遍）。awaiting_integration_recheck
    这一步"强制先复核"不能无脑跳过这个重置——必须先让lifestyle_agent_node重新跑一遍，
    不能绕过直接回integration_agent_node（那样lifestyle_agent_done=False这个信号
    就白设置了，永远不会被消费）。"""
    state = {
        "project_id": isolated_project,
        "awaiting_integration_recheck": True,
        "integration_retry_count": 1,
        "lifestyle_agent_done": False,
        "pitfall_agent_done": False,
    }
    assert _route_integration_phase(state) == "lifestyle_agent_node"


def test_integration_phase_recheck_routes_to_pitfall_when_only_pitfall_reset(isolated_project):
    """同上，但lifestyle已经重新跑完（True），只有pitfall_agent_done还是False——
    应该路由到pitfall_agent_node，不是直接回integration_agent_node，也不是回lifestyle。"""
    state = {
        "project_id": isolated_project,
        "awaiting_integration_recheck": True,
        "integration_retry_count": 1,
        "lifestyle_agent_done": True,
        "pitfall_agent_done": False,
    }
    assert _route_integration_phase(state) == "pitfall_agent_node"


def test_integration_phase_routes_normally_without_recheck_flag(isolated_project):
    """对照组：没有awaiting_integration_recheck标志时，行为应该和修复前一致——
    有pending conflict且未达上限就打回目标agent。"""
    state = {"project_id": isolated_project, "integration_retry_count": 0}
    store.report_conflict(isolated_project, actor="integration_agent", target="lifestyle_agent.food", reason="太远了")

    assert _route_integration_phase(state) == "lifestyle_agent_node"


def test_integration_phase_routes_to_route_draft_for_route_target(isolated_project):
    state = {"project_id": isolated_project, "integration_retry_count": 0}
    store.report_conflict(isolated_project, actor="integration_agent", target="route_agent", reason="路线有问题")

    assert _route_integration_phase(state) == "route_draft"


def test_integration_retry_count_survives_new_conflict_id(isolated_project):
    """retry_count重置的bug场景：每次report_conflict都创建新id的记录，如果拿
    conflict.retry_count判断上限，复核后问题依旧、重新report出的新conflict会从0计数，
    MAX_RETRY形同虚设。这里验证：即使每轮都是全新的conflict id，只要
    state["integration_retry_count"]累计到MAX_RETRY，就必须强制终结，不能无限打回。
    """
    # 模拟MAX_RETRY轮"打回->修复->复核发现问题依旧->重新report"，每轮都是新conflict id
    for round_idx in range(MAX_RETRY):
        record = store.report_conflict(
            isolated_project, actor="integration_agent", target="lifestyle_agent.food", reason=f"第{round_idx}轮仍有问题"
        )
        state = {"project_id": isolated_project, "integration_retry_count": round_idx}
        next_node = _route_integration_phase(state)
        assert next_node == "lifestyle_agent_node"
        # 模拟修复+进integration复核这两步，产生一条新的conflict（新id，旧的没被resolve）
        store.resolve_conflict(isolated_project, record.id)  # 旧的解决（模拟被覆盖/失效）

    # 到达MAX_RETRY次后，哪怕这一条conflict的retry_count字段是0（全新id），也必须强制终结
    new_record = store.report_conflict(
        isolated_project, actor="integration_agent", target="lifestyle_agent.food", reason="还是有问题"
    )
    assert new_record.retry_count == 0  # 印证"新conflict的retry_count确实是从0开始"这个前提

    state = {"project_id": isolated_project, "integration_retry_count": MAX_RETRY}
    assert _route_integration_phase(state) == "integration_agent_node"

    # get_pending_conflict只返回status=="pending"的记录，force_finalize后状态变了，
    # 所以查不到才是符合预期的——直接读changelog里最新一条conflict记录确认真的被标记了
    conflicts = store.read_workspace(isolated_project, "conflicts.jsonl")
    assert conflicts[-1]["status"] == "force_finalized"
    assert store.get_pending_conflict(isolated_project) is None


def test_integration_phase_no_conflict_goes_to_integration(isolated_project):
    state = {"project_id": isolated_project, "integration_retry_count": 0}
    assert _route_integration_phase(state) == "integration_agent_node"


# ---------------------------------------------------------------------------
# main_agent_router：mode分发本身（见CLAUDE.md已知问题第6条、docs/ISSUES.md第49条
# 已知局限第2点）——子路由函数_route_plan_mode/_route_edit_mode各自都有大量测试覆盖，
# 但"根据state['mode']字段分发到哪一个子路由"这一步之前从未被专门测过。这里用monkeypatch
# 把两个子路由函数换成可识别的哨兵返回值，只验证分发这一步本身，不重复测子路由内部逻辑。
# ---------------------------------------------------------------------------


def test_main_agent_router_dispatches_to_plan_mode(monkeypatch):
    monkeypatch.setattr(orchestrator, "_route_plan_mode", lambda state: "PLAN_SENTINEL")
    monkeypatch.setattr(orchestrator, "_route_edit_mode", lambda state: "EDIT_SENTINEL")

    assert orchestrator.main_agent_router({"mode": "plan"}) == "PLAN_SENTINEL"


def test_main_agent_router_dispatches_to_edit_mode(monkeypatch):
    monkeypatch.setattr(orchestrator, "_route_plan_mode", lambda state: "PLAN_SENTINEL")
    monkeypatch.setattr(orchestrator, "_route_edit_mode", lambda state: "EDIT_SENTINEL")

    assert orchestrator.main_agent_router({"mode": "edit"}) == "EDIT_SENTINEL"


def test_main_agent_router_defaults_to_plan_mode_when_mode_missing(monkeypatch):
    """state.get("mode", "plan")——mode字段缺失时的默认值是"plan"，不是"edit"。
    CLI/API等一次性传入requirements的既有调用方（run_plan）从没有显式设过mode
    以外的默认值依赖，这条测试确保这个隐式默认值不会被无意间改动。"""
    monkeypatch.setattr(orchestrator, "_route_plan_mode", lambda state: "PLAN_SENTINEL")
    monkeypatch.setattr(orchestrator, "_route_edit_mode", lambda state: "EDIT_SENTINEL")

    assert orchestrator.main_agent_router({}) == "PLAN_SENTINEL"
