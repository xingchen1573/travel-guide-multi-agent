"""飞书WebSocket长连接客户端：只做协议适配层，把lark_oapi SDK的事件对象/HTTP客户端
包装成service.py能用的最小接口（handle_message + ReplyChannel），不含任何业务判断——
业务逻辑全部在service.py里，这里只负责"消息怎么收、怎么发、怎么编辑"。

选WebSocket而不是Webhook：不需要公网可达地址，本地/内网开发就能收到飞书事件，见
`docs/reference-archives/2026-07-feishu-integration-lessons.md` §1.1。真实凭证从FEISHU_APP_ID/FEISHU_APP_SECRET读取，FEISHU_ENABLE_REAL_SEND
关闭时只打印日志不真实调用飞书API（本地开发/无真实凭证时的降级模式，跟AMAP_MCP_API_KEY等
项目里其他"没有key就不阻塞主流程"的既有模式一致）。
"""
from __future__ import annotations

import json
import os

import lark_oapi as lark

from src.channels.feishu import service

FEISHU_APP_ID = os.environ.get("FEISHU_APP_ID")
FEISHU_APP_SECRET = os.environ.get("FEISHU_APP_SECRET")
FEISHU_ENABLE_REAL_SEND = os.environ.get("FEISHU_ENABLE_REAL_SEND", "false").lower() == "true"


def _card_content(text: str) -> str:
    """把纯文字包装成一张只有一段lark_md的卡片JSON。真实联调（2026-07-25）发现飞书的
    PatchMessage接口只支持编辑interactive（卡片）类型的消息，text类型消息发出去之后不能再
    编辑（真实报错：code=230001 "This message is NOT a card."）——send/edit必须用同一种
    msg_type，所以连带send()也要改成卡片。用户在群里看到的视觉效果和纯文本消息没有区别（卡片
    里只放了一段文字，没有任何按钮/交互控件），不影响`docs/ARCHITECTURE.md`第14节"不做卡片
    按钮交互"这条设计决策——那条决策说的是HITL确认不用卡片上的按钮，跟这里"borrow卡片消息类型
    只是为了让消息能被原地编辑"是两回事。text字段改用lark_md（原plain_text不渲染markdown，
    导致**加粗**/##标题等在飞书里显示成原始符号）。"""
    return json.dumps({
        "config": {"wide_screen_mode": True},
        "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": text}}],
    })


class FeishuReplyChannel(service.ReplyChannel):
    """把service.ReplyChannel的send/edit映射到飞书im/v1消息API。FEISHU_ENABLE_REAL_SEND
    关闭时（没有真实凭证或本地开发）只打印到日志，不真实调用飞书API——跟项目里其余
    "没有key时优雅降级"的模式一致，也是run_feishu_ws_probe这类本地自测脚本的运行前提。
    """

    def __init__(self, client: "lark.Client", chat_id: str):
        self._client = client
        self._chat_id = chat_id

    def send(self, text: str) -> str:
        if not FEISHU_ENABLE_REAL_SEND:
            fake_id = f"fake-msg-{abs(hash(text)) % 100000}"
            print(f"[飞书mock发送] chat={self._chat_id} message_id={fake_id}: {text}")
            return fake_id

        request = (
            lark.im.v1.CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                lark.im.v1.CreateMessageRequestBody.builder()
                .receive_id(self._chat_id)
                .msg_type("interactive")
                .content(_card_content(text))
                .build()
            )
            .build()
        )
        response = self._client.im.v1.message.create(request)
        if not response.success():
            raise RuntimeError(f"飞书发消息失败：code={response.code} msg={response.msg}")
        return response.data.message_id

    def edit(self, message_id: str, text: str) -> None:
        if not FEISHU_ENABLE_REAL_SEND:
            print(f"[飞书mock编辑] message_id={message_id}: {text}")
            return

        request = (
            lark.im.v1.PatchMessageRequest.builder()
            .message_id(message_id)
            .request_body(
                lark.im.v1.PatchMessageRequestBody.builder()
                .content(_card_content(text))
                .build()
            )
            .build()
        )
        response = self._client.im.v1.message.patch(request)
        if not response.success():
            raise RuntimeError(f"飞书编辑消息失败：code={response.code} msg={response.msg}")

    def send_image(self, image_path: str) -> str:
        """海报推送用（2026-07-26新增）：先调im/v1/images上传拿image_key，再发一条
        msg_type="image"的消息。跟send()/edit()的卡片消息是完全不同的两个API——图片
        消息不支持像卡片那样原地编辑，所以海报固定用"新发一条消息"，不接进
        进度卡片的编辑逻辑里。签名参照lark_oapi SDK源码（CreateImageRequest/
        CreateImageRequestBody）手工核对，跟本文件其余方法一样，**没有做真实联调**，
        见docs/ARCHITECTURE.md第14节已知局限。
        """
        if not FEISHU_ENABLE_REAL_SEND:
            fake_id = f"fake-image-msg-{abs(hash(image_path)) % 100000}"
            print(f"[飞书mock发图片] chat={self._chat_id} image_path={image_path} message_id={fake_id}")
            return fake_id

        with open(image_path, "rb") as f:
            upload_request = (
                lark.im.v1.CreateImageRequest.builder()
                .request_body(
                    lark.im.v1.CreateImageRequestBody.builder()
                    .image_type("message")
                    .image(f)
                    .build()
                )
                .build()
            )
            upload_response = self._client.im.v1.image.create(upload_request)

        if not upload_response.success():
            raise RuntimeError(f"飞书上传图片失败：code={upload_response.code} msg={upload_response.msg}")
        image_key = upload_response.data.image_key

        request = (
            lark.im.v1.CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                lark.im.v1.CreateMessageRequestBody.builder()
                .receive_id(self._chat_id)
                .msg_type("image")
                .content(json.dumps({"image_key": image_key}))
                .build()
            )
            .build()
        )
        response = self._client.im.v1.message.create(request)
        if not response.success():
            raise RuntimeError(f"飞书发图片消息失败：code={response.code} msg={response.msg}")
        return response.data.message_id


def _normalize_ws_message_event(event: "lark.im.v1.P2ImMessageReceiveV1") -> tuple[str, str]:
    """把SDK的事件对象拍平成(chat_id, text)——这是"进料口"和业务代码之间的边界，
    业务代码（service.handle_message）不感知这条消息是从WS来的还是以后可能接入的Webhook来的，
    跟 `docs/reference-archives/2026-07-feishu-integration-lessons.md` §1.1 记录的外部经验一致。只处理纯文本消息，其他消息类型（图片/富文本等）
    这一期不支持，直接忽略。"""
    import json as _json

    message = event.event.message
    chat_id = message.chat_id
    text = ""
    if message.message_type == "text":
        content = _json.loads(message.content or "{}")
        text = content.get("text", "")
    return chat_id, text


def build_ws_client() -> "lark.ws.Client":
    """构建飞书WS长连接客户端。调用方（run_feishu_worker.py）负责调用.start()阻塞维持连接
    （见 `docs/reference-archives/2026-07-feishu-integration-lessons.md` §1.6：这是一个阻塞调用，必须跑在独立于其他Web服务的进程/线程里）。"""
    api_client = build_api_client()

    def on_message_receive(event: "lark.im.v1.P2ImMessageReceiveV1") -> None:
        chat_id, text = _normalize_ws_message_event(event)
        if not text:
            return
        channel = FeishuReplyChannel(api_client, chat_id)
        service.handle_message(chat_id, text, channel)

    event_handler = (
        lark.EventDispatcherHandler.builder(
            os.environ.get("FEISHU_VERIFICATION_TOKEN", ""),
            os.environ.get("FEISHU_ENCRYPT_KEY", ""),
        )
        .register_p2_im_message_receive_v1(on_message_receive)
        .build()
    )

    return lark.ws.Client(
        FEISHU_APP_ID,
        FEISHU_APP_SECRET,
        event_handler=event_handler,
        log_level=lark.LogLevel.INFO,
    )


def build_api_client() -> "lark.Client":
    """构建飞书API客户端（收发消息用），不建立WS长连接。build_ws_client内部用它来发送/编辑
    消息；主动提醒机制的推送轮询（run_feishu_worker.py）也复用这个函数单独构建一个客户端
    实例——`lark.Client`本身是无状态的HTTP客户端封装，多建一个实例不需要跟WS事件回调共享
    同一个对象。"""
    if not (FEISHU_APP_ID and FEISHU_APP_SECRET):
        raise RuntimeError(
            "缺少飞书应用凭证，请检查 .env 中的 FEISHU_APP_ID / FEISHU_APP_SECRET"
        )

    return lark.Client.builder().app_id(FEISHU_APP_ID).app_secret(FEISHU_APP_SECRET).build()
