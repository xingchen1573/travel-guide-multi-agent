"""实时agent专用工具：answer_query / propose_patch / get_current_time / create_reminder /
list_reminders / cancel_reminder / update_reminder。

answer_query/propose_patch/create_reminder跟src/tools/guide_tools.py同款captured-dict
边车容器模式（见docs/ISSUES.md关于react-agent无法直接返回结构化数据的说明）——调用方
（src/orchestrator.py的realtime_agent_node）传入一个可变dict，agent调用其中一个工具时
把结果写进这个dict，节点函数据此判断走"纯问答"/"发起修改"/"创建提醒"三条路径中的哪一条。

三个"结束本轮"的工具选其一：只提交答案(answer_query)、提交一个待审批的patch(propose_patch)、
或创建一条提醒任务(create_reminder)，不直接写入任何workspace文件——实时agent对公共区
的写入权限只在主agent/用户审批通过后才真正生效（见docs/ORCHESTRATION.md第3.2节），
这里绝不能给它绑真正的write工具。create_reminder写的是reminders.json（调度元数据，
不是workspace文件，见src/channels/feishu/reminders.py），不受这条约束限制。

get_current_time是主动提醒机制v1（docs/ARCHITECTURE.md第14.1节）新增的纯查询工具，不写入
captured，只是让模型能查到当前时刻——时间语言解析交给LLM（本来就擅长"一小时后"这类相对
时间推算），工具只做"傻瓜式"接收结构化输入+持久化，跟项目里"agent负责推理、tool负责纯
执行"的一贯分工一致。

list_reminders/cancel_reminder/update_reminder是2026-07-26新增（取消/修改/重复规则，
此前明确排入下一阶段，见docs/ARCHITECTURE.md第14.1节）——跟create_reminder不同，这三个
不是"结束本轮"的工具：模型往往需要先list_reminders看看有哪些提醒（拿到具体的id），再
决定要不要cancel/update其中一条，中间这一步查询不应该提前终止整个对话轮次。因此这三个
工具**不写入captured["mode"]**，调用后模型可以接着调用answer_query把操作结果转述给用户
（这也是prompt里要求的收尾方式），跟"三选一互斥收尾"的另外三个工具是两类不同的定位。
"""
from __future__ import annotations

from datetime import datetime, timezone

from langchain_core.tools import tool

from src.channels.feishu import bindings, reminders


def make_realtime_tools(project_id: str, captured: dict) -> list:
    @tool
    def answer_query(text: str) -> str:
        """提交一次纯问答的回答（不涉及修改任何内容）。调用后本轮任务结束，
        不要再调用propose_patch或其他工具。"""
        captured["mode"] = "answer"
        captured["text"] = text
        return "回答已提交。"

    @tool
    def propose_patch(field: str, day: int, new_recommendation: str, reason: str) -> str:
        """提交一个待审批的修改建议，不会立即生效——需要主agent/用户审批通过后系统才会
        真正写入。field必须是food/outfit/lodging/play之一，day是第几天，
        new_recommendation是更新后的建议文本，reason说明为什么需要这次更新
        （比如"该餐厅已确认长期停业"）。调用后本轮任务结束，不要再调用answer_query
        或其他工具，也不要在文字里说"已经帮你改好了"——修改是否生效取决于审批结果，
        不是你能直接决定的。"""
        captured["mode"] = "patch"
        captured["patch"] = {
            "field": field,
            "day": day,
            "new_recommendation": new_recommendation,
            "reason": reason,
        }
        return "修改建议已提交，等待审批后才会真正写入。"

    @tool
    def get_current_time() -> str:
        """返回当前时间（ISO格式，UTC时区）。用户说"一小时后""明天早上8点"这类相对时间时，
        先调用这个工具拿到当前时刻，再自己推算出trigger_at需要的绝对时间戳，不要凭自己的
        知识猜测现在是几点。"""
        return datetime.now(timezone.utc).isoformat()

    @tool
    def create_reminder(trigger_at: str, message: str, repeat_interval_hours: float = 0) -> str:
        """创建一条到点后会通过飞书推送给用户的提醒任务。trigger_at必须是你自己推算好的
        绝对时间戳（ISO格式，如"2026-07-25T15:00:00+00:00"），不要传相对时间描述这类
        非结构化文本；message是提醒文案（如"该出发去高铁站了"）。

        repeat_interval_hours是可选的重复间隔（单位小时），默认0表示一次性提醒（只触发
        一次）；如果用户要求的是"每天提醒我""每隔6小时提醒我"这类重复规则，传入对应的
        小时数（"每天"传24，"每隔6小时"传6），提醒会在每次触发后自动推进到下一个周期，
        不需要用户重新创建。

        调用后本轮任务结束，不要再调用answer_query/propose_patch等其他工具（如果用户
        紧接着还想看看当前有哪些提醒，那是下一轮的list_reminders，不是这一轮）。

        这个工具只能在当前project已经绑定了飞书群的情况下才会生效——如果没有绑定
        （比如纯CLI跑的project），会返回明确的失败提示，如实告知用户"当前渠道不支持
        创建提醒"，不要假装已经创建成功。"""
        chat_id = bindings.get_chat_id_for_project(project_id)
        if not chat_id:
            result_text = "创建提醒失败：当前project没有绑定飞书群，无法在到点时推送提醒。"
        else:
            reminders.create_reminder(
                project_id,
                chat_id,
                trigger_at,
                message,
                repeat_interval_hours=repeat_interval_hours or None,
            )
            if repeat_interval_hours:
                result_text = (
                    f"提醒已创建，将从 {trigger_at} 开始，每隔{repeat_interval_hours}小时"
                    f"通过飞书重复推送。"
                )
            else:
                result_text = f"提醒已创建，将在 {trigger_at} 通过飞书推送。"
        captured["mode"] = "reminder"
        captured["text"] = result_text
        return result_text

    @tool
    def list_reminders() -> str:
        """查看当前project下所有还没触发的提醒（不包括已经推送过/已放弃重试/已取消的）。
        用户问"我还有哪些提醒""帮我看看设置了什么提醒"时调用这个工具，拿到结果后需要
        接着调用answer_query把内容转述给用户——这个工具本身不结束本轮任务。返回的每条
        提醒都带id，如果用户接下来要取消/修改其中一条，需要用这里返回的id去调用
        cancel_reminder/update_reminder，不要自己编造id。"""
        pending = reminders.list_reminders_for_project(project_id)
        if not pending:
            return "当前没有待触发的提醒。"
        lines = []
        for record in pending:
            repeat = record.get("repeat_interval_hours")
            repeat_desc = f"（每隔{repeat}小时重复）" if repeat else ""
            lines.append(
                f"id={record['id']}：{record['trigger_at']} - {record['message']}{repeat_desc}"
            )
        return "\n".join(lines)

    @tool
    def cancel_reminder_tool(reminder_id: str) -> str:
        """取消一条待触发的提醒，reminder_id必须来自list_reminders返回结果里的id，
        不要凭猜测/记忆编造id。调用后需要接着调用answer_query把取消结果告知用户——
        这个工具本身不结束本轮任务。如果返回失败（提醒不存在，或已经推送/已经取消过），
        如实告知用户，不要假装取消成功了。"""
        ok = reminders.cancel_reminder(project_id, reminder_id)
        return "取消成功。" if ok else "取消失败：没有找到这条待触发的提醒（可能已经推送过、已经取消过，或id不对）。"

    @tool
    def update_reminder_tool(
        reminder_id: str,
        trigger_at: str = "",
        message: str = "",
        repeat_interval_hours: float = -1,
    ) -> str:
        """修改一条待触发提醒的时间/文案/重复规则，reminder_id必须来自list_reminders
        返回结果里的id，不要凭猜测编造。只传用户真正要改的字段——trigger_at/message
        留空字符串表示不改，repeat_interval_hours传-1表示不改重复规则、传0表示改成
        一次性提醒（清除重复规则）、传正数表示改成对应小时数的重复间隔。调用后需要接着
        调用answer_query把修改结果告知用户——这个工具本身不结束本轮任务。如果返回失败
        （提醒不存在，或已经推送/已经取消过），如实告知用户，不要假装修改成功了。"""
        update_kwargs: dict = {
            "trigger_at": trigger_at or None,
            "message": message or None,
        }
        if repeat_interval_hours == 0:
            update_kwargs["repeat_interval_hours"] = None
        elif repeat_interval_hours > 0:
            update_kwargs["repeat_interval_hours"] = repeat_interval_hours
        # repeat_interval_hours < 0（默认-1）：不传这个字段，沿用reminders.update_reminder
        # 自己"不传=不改重复规则"的默认哨兵值，工具这层不需要知道对方内部怎么实现。
        ok = reminders.update_reminder(project_id, reminder_id, **update_kwargs)
        return "修改成功。" if ok else "修改失败：没有找到这条待触发的提醒（可能已经推送过、已经取消过，或id不对）。"

    return [
        answer_query,
        propose_patch,
        get_current_time,
        create_reminder,
        list_reminders,
        cancel_reminder_tool,
        update_reminder_tool,
    ]
