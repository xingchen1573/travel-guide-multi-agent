"""飞书群↔project绑定：一个飞书chat_id对应一份旅程历史，其中一个是"活跃"project
（`/grilling`访谈定案，2026-07-26，见docs/ARCHITECTURE.md第14.4节）——只支持"旧旅程
完成很久后再发起一次全新的旅程"这个场景，不做"同一时刻并行管理多个活跃project"。

第一条消息自动创建绑定（project_id用不带序号的确定性命名`f"feishu-{chat_id}"`，
保持跟历史行为一致，不影响真实存量数据）；之后每次用户确认发起新旅程
（service.py::_handle_new_journey_offer_response），`start_new_journey`生成新的、
序号式命名的project_id（`f"feishu-{chat_id}-{下一个序号}"`），追加进`history`并设为
`active_project_id`。旧project对该飞书群不再可达（数据完整保留，只能通过管理后台查看，
不支持"切回"）。

数据落在跨project的全局文件data/feishu/bindings.json——这是渠道层自己的路由状态，不是
核心业务数据，不进任何单个project的workspace/，不经过src/workspace/store.py的权限校验
体系（那套体系管的是"project内部谁能写哪个文件"，这里是"渠道消息路由到哪个project"，
是完全不同层面的东西）。

字段说明：
- active_project_id：当前活跃的project_id，`handle_message`按这个id路由消息。
- history：这个chat_id曾经拥有过的全部project_id列表（含当前active的），纯留痕，
  不提供"切回"交互；`get_chat_id_for_project`反查时任何一个历史id都能查到这个chat_id
  （reminder等机制不受active与否影响，因为它们创建时就固化了chat_id，不依赖这条推导）。
- awaiting：这个project当前的channel图是否正在等一个具体的interrupt() resume：
  - None：没有pending的interrupt，下一条消息应该发起一次全新的graph.invoke(initial_state, ...)
  - "requirements"：正在等多轮需求收集的下一个回复（requirements_gather_node的interrupt）
  - "skeleton_confirm"：正在等首次骨架确认/调整的回复（await_skeleton_confirm_node的interrupt）
  - "route_structure_confirm"：正在等确认后路线结构候选的采用/保留/调整回复
  - "realtime_approval"：正在等陪伴阶段实时agent写入审批的回复（await_realtime_approval_node）
  - "poster_offer"：完成消息主动问了"要不要生成海报"，正在等这句话的回复（不经过graph）
  - "new_journey_offer"：检测到用户可能想发起新旅程，反问确认后正在等这句话的回复
    （不经过graph，见service.py::_handle_new_journey_offer_response）
- pending_new_journey_message：仅`awaiting=="new_journey_offer"`期间非空——暂存触发
  这次检测的原始消息（往往已经包含真实旅行信息，如"我想去大理玩3天"），用户确认后直接
  喂给新project的`run_channel_start`，不逼用户复述一遍。

旧数据迁移：真实data/feishu/bindings.json可能还留着旧格式记录
（`{"project_id":..., "awaiting":...}`，2026-07-25/26真实联调产生），采用**就地静默
升级**——读取路径里发现旧格式（没有`active_project_id`键）就原地转换并写回，不单独
维护一次性迁移脚本。转换规则：`active_project_id`/`history`直接取旧的`project_id`值
（保留真实存量数据完整可用，不重新生成新id）。
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Optional

from src.config import DATA_DIR

BINDINGS_PATH = DATA_DIR / "feishu" / "bindings.json"

_lock = threading.Lock()


def _read_all(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _write_all(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _migrate_binding(binding: dict) -> tuple[dict, bool]:
    """把单条绑定记录从旧schema（project_id）原地升级到新schema（active_project_id+
    history）。已经是新schema时原样返回（changed=False）。"""
    if "active_project_id" in binding:
        return binding, False
    project_id = binding.get("project_id")
    migrated = {
        "active_project_id": project_id,
        "history": [project_id] if project_id else [],
        "awaiting": binding.get("awaiting"),
        "pending_new_journey_message": binding.get("pending_new_journey_message"),
    }
    return migrated, True


def _read_all_migrated(path: Path) -> tuple[dict, bool]:
    data = _read_all(path)
    changed = False
    for chat_id, binding in list(data.items()):
        migrated, was_changed = _migrate_binding(binding)
        if was_changed:
            data[chat_id] = migrated
            changed = True
    return data, changed


def get_binding(chat_id: str) -> Optional[dict]:
    with _lock:
        data, changed = _read_all_migrated(BINDINGS_PATH)
        if changed:
            _write_all(BINDINGS_PATH, data)
        return data.get(chat_id)


def get_or_create_binding(chat_id: str) -> dict:
    """没有绑定时新建一条——第一个project_id按chat_id确定性生成（f"feishu-{chat_id}"，
    不带序号，跟历史行为一致），一个chat_id对应一份history，awaiting默认为None。
    已存在绑定时直接返回（若是旧格式则顺带就地迁移），不做任何其它修改（幂等）。

    2026-08-18新增：首次创建时标记_is_new_user=True，供service.py发送欢迎消息（方案A）。"""
    with _lock:
        data, changed = _read_all_migrated(BINDINGS_PATH)
        if chat_id not in data:
            project_id = f"feishu-{chat_id}"
            data[chat_id] = {
                "active_project_id": project_id,
                "history": [project_id],
                "awaiting": None,
                "pending_new_journey_message": None,
                "_is_new_user": True,  # 🆕 标记为新用户
            }
            changed = True
        if changed:
            _write_all(BINDINGS_PATH, data)
        return data[chat_id]


def clear_new_user_flag(chat_id: str) -> None:
    """清除新用户标志位。欢迎消息发送后调用，确保不会重复发送。chat_id必须已经绑定过。

    2026-08-18新增：配合get_or_create_binding的_is_new_user标志位。"""
    with _lock:
        data, _ = _read_all_migrated(BINDINGS_PATH)
        if chat_id not in data:
            raise ValueError(f"chat_id={chat_id!r}还没有绑定，不能清除新用户标志")
        data[chat_id].pop("_is_new_user", None)
        _write_all(BINDINGS_PATH, data)


def set_awaiting(chat_id: str, awaiting: Optional[str]) -> None:
    """更新这个chat绑定的awaiting标记。chat_id必须已经绑定过（先调用
    get_or_create_binding），否则说明调用顺序有误，直接报错而不是静默创建一条
    awaiting状态不明的绑定。"""
    with _lock:
        data, _ = _read_all_migrated(BINDINGS_PATH)
        if chat_id not in data:
            raise ValueError(f"chat_id={chat_id!r}还没有绑定，不能设置awaiting状态")
        data[chat_id]["awaiting"] = awaiting
        _write_all(BINDINGS_PATH, data)


def set_pending_new_journey_message(chat_id: str, message: str) -> None:
    """暂存触发"发起新旅程"检测的原始消息，供用户确认后直接喂给新project的
    run_channel_start，不逼用户重复一遍。chat_id必须已经绑定过。"""
    with _lock:
        data, _ = _read_all_migrated(BINDINGS_PATH)
        if chat_id not in data:
            raise ValueError(f"chat_id={chat_id!r}还没有绑定，不能暂存新旅程消息")
        data[chat_id]["pending_new_journey_message"] = message
        _write_all(BINDINGS_PATH, data)


def pop_pending_new_journey_message(chat_id: str) -> Optional[str]:
    """取出并清空暂存的原始消息——不管用户最终确认还是拒绝，这条暂存值都应该在
    这一轮处理完之后被清空，不能跨轮次残留。chat_id必须已经绑定过。"""
    with _lock:
        data, _ = _read_all_migrated(BINDINGS_PATH)
        if chat_id not in data:
            raise ValueError(f"chat_id={chat_id!r}还没有绑定，不能读取新旅程消息")
        message = data[chat_id].get("pending_new_journey_message")
        data[chat_id]["pending_new_journey_message"] = None
        _write_all(BINDINGS_PATH, data)
        return message


def start_new_journey(chat_id: str) -> str:
    """用户确认要发起一次新旅程后调用——生成新project_id（序号式命名
    `f"feishu-{chat_id}-{下一个序号}"`，从2开始，因为第一个project已经占了不带
    序号的名字），追加进history，设为active，awaiting重置为None（下一条消息会是
    新project的第一条，走run_channel_start）。**不支持切回**——旧project对这个
    chat_id不再可达（数据完整保留，只能通过管理后台查看）。chat_id必须已经绑定过。
    """
    with _lock:
        data, _ = _read_all_migrated(BINDINGS_PATH)
        if chat_id not in data:
            raise ValueError(f"chat_id={chat_id!r}还没有绑定，不能发起新旅程")
        binding = data[chat_id]
        new_project_id = f"feishu-{chat_id}-{len(binding['history']) + 1}"
        binding["history"].append(new_project_id)
        binding["active_project_id"] = new_project_id
        binding["awaiting"] = None
        binding["pending_new_journey_message"] = None
        _write_all(BINDINGS_PATH, data)
        return new_project_id


def get_chat_id_for_project(project_id: str) -> Optional[str]:
    """按project_id反查chat_id——bindings.json本来是chat_id→project_id方向的映射，
    这里做反向查找。供主动提醒机制（docs/ARCHITECTURE.md第14.1节）的create_reminder
    工具使用：模型只知道自己在哪个project里，需要反查出该project当前绑定的飞书群
    才能确定提醒要推给谁。在`history`列表里查找（不只是`active_project_id`）——
    即使某个project已经不是active的了（旧旅程），它创建时固化的chat_id映射关系
    依然应该能被正确反查到，不受"一个群多次旅程"这条新功能影响。project从未绑定过
    飞书群（比如纯CLI跑的project）时返回None，调用方需要据此判断"这个project无法
    创建提醒"，不是报错。"""
    with _lock:
        data, changed = _read_all_migrated(BINDINGS_PATH)
        if changed:
            _write_all(BINDINGS_PATH, data)
        for chat_id, binding in data.items():
            if project_id in binding.get("history", []):
                return chat_id
        return None


def list_bindings() -> dict[str, dict]:
    """列出全部chat_id→binding映射，供管理后台飞书运维可视化只读展示
    （docs/ARCHITECTURE.md第13.2节域8）使用。直接返回bindings.json的完整内容
    （chat_id为key，已按新schema迁移），不做任何过滤/排序——排序/enrichment是调用方
    （API层）的职责，这里只负责"读全部"这一件事，跟get_binding/get_or_create_binding
    的单条读取是同一个模块内两种不同粒度的只读访问。"""
    with _lock:
        data, changed = _read_all_migrated(BINDINGS_PATH)
        if changed:
            _write_all(BINDINGS_PATH, data)
        return data


def unbind_project(project_id: str) -> list[str]:
    """管理后台软删除project时调用——把所有指向这个project_id的active_project_id
    清成None，让该飞书群不再能路由消息到一个已经被删除的project。不动history
    （get_chat_id_for_project反查依赖history，主动提醒等机制不受这次解绑影响，
    见下方说明），也不删除chat_id对应的整条绑定记录本身（这个群本身依然存在，
    只是当前活跃project没了）。

    返回受影响的chat_id列表（可能为空），供调用方记日志/写测试断言。幂等：
    project_id若不是任何绑定的active_project_id，返回空列表，不报错（软删除
    一个从未绑定过飞书群的project是完全正常的场景，不是异常）。

    已知限制：reminder_poller等主动提醒机制通过get_chat_id_for_project按history
    反查chat_id，不看active_project_id，所以一个project被删除后，它名下已创建
    的提醒仍然会照常触发推送——这是刻意的权衡（详见docs/ARCHITECTURE.md），不在
    这个函数里处理。
    """
    with _lock:
        data, _ = _read_all_migrated(BINDINGS_PATH)
        affected: list[str] = []
        for chat_id, binding in data.items():
            if binding.get("active_project_id") == project_id:
                binding["active_project_id"] = None
                affected.append(chat_id)
        if affected:
            _write_all(BINDINGS_PATH, data)
        return affected
