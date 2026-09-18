"""飞书WS长连接worker：独立可执行进程，跟uvicorn等其他Web服务分开跑。

为什么要独立进程：WS长连接的.start()是一个阻塞调用，会一直占住线程/进程（见
`docs/reference-archives/2026-07-feishu-integration-lessons.md` §1.6）。如果跟主Web服务塞进同一个多worker/热重载的进程里，会出现同一个
app_id重复建立多条长连接，飞书那边报冲突或消息被重复投递。

文件锁防止手滑重复启动多个worker实例（同一个app_id只应该有一条WS长连接）：
POSIX用fcntl.flock，Windows用msvcrt.locking，跟docs/DEV_SETUP.md记录的其他
Windows平台注意事项是同一类"跨平台不透明"问题。拿不到锁直接以exit code 3退出，
不做等待重试——已经有一个worker在跑，说明上层部署脚本手滑重复启动了，等待没有意义。

主动提醒机制v1（docs/ARCHITECTURE.md第14.1节）的调度器也挂在这个进程里：WS长连接的
.start()阻塞主线程之前，先起一个独立的后台轮询线程（src/channels/feishu/reminder_poller.py），
每60秒扫一次所有project的reminders.json，到期任务用这个进程持有的飞书连接推送——
不新增独立进程，理由是推送提醒本身需要主动发消息（不是被动回复），而这个进程本来就
保持着WS长连接，调度和推送在同一进程里天然闭环。

飞书运维可视化v1（docs/ARCHITECTURE.md第13.2节域8）的心跳线程同样挂在这里：
src/channels/feishu/health.py每20秒写一次data/feishu/worker_heartbeat.json，管理后台
（独立的uvicorn进程）据此判断这个worker进程是否还活着、上次真正工作是什么时候——
只是"进程心跳"，不是"WS连接本身在飞书那端真的通"的证明，见health.py模块docstring。

用法：
    PYTHONIOENCODING=utf-8 python run_feishu_worker.py

需要在.env里配置FEISHU_APP_ID/FEISHU_APP_SECRET（真实发消息还需要FEISHU_ENABLE_REAL_SEND=true，
见src/channels/feishu/ws_client.py）。
"""
from __future__ import annotations

import sys

from src.config import DATA_DIR

LOCK_PATH = DATA_DIR / "feishu_ws_worker.lock"

_lock_file = None  # 模块级引用：文件对象在worker存活期间必须一直打开，锁才持续生效


def _acquire_lock() -> bool:
    global _lock_file
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    _lock_file = open(LOCK_PATH, "w")
    try:
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(_lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(_lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        _lock_file.close()
        _lock_file = None
        return False


def main() -> None:
    if not _acquire_lock():
        print(f"已经有一个飞书worker实例在运行（锁文件：{LOCK_PATH}），本次启动直接退出。")
        sys.exit(3)

    from src.channels.feishu.health import start_heartbeat_thread
    from src.channels.feishu.reminder_poller import start_polling_thread
    from src.channels.feishu.ws_client import FeishuReplyChannel, build_api_client, build_ws_client
    from src.execution_state import JobService, RuntimeManager, environment_name

    runtime_manager = RuntimeManager(
        service=JobService(),
        role="feishu_worker",
        environment=environment_name(),
        enabled=True,
        coordinator=False,
    )
    runtime_manager.start()

    try:
        api_client = build_api_client()

        def _send_reminder(chat_id: str, text: str) -> None:
            # 复用ws_client.py里已有的发送逻辑（FeishuReplyChannel.send），message_id用不上
            # 直接丢弃——提醒推送是主动发一条新消息，不像进度轮询那样需要原地编辑同一条消息。
            FeishuReplyChannel(api_client, chat_id).send(text)

        print("心跳线程启动中…（每20秒写一次worker_heartbeat.json，供管理后台运维可视化查询）")
        start_heartbeat_thread()

        print("主动提醒轮询线程启动中…（每60秒扫一次到期任务）")
        start_polling_thread(_send_reminder)

        print("飞书WS长连接worker启动中…")
        client = build_ws_client()
        client.start()  # 阻塞调用，一直维持长连接直到进程退出
    finally:
        runtime_manager.stop()


if __name__ == "__main__":
    main()
