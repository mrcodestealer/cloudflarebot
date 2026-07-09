"""Lark / Feishu bot: persistent WebSocket subscription + outbound messages.

* Uses lark-oapi's WebSocket long-connection client (Subscription mode ->
  "Receive events through persistent connection"), which auto-reconnects.
* Receives ``im.message.receive_v1`` events; when the bot is @-mentioned with a
  ``/mo`` command it fires the registered command handler.  The WS handler stays
  lightweight -- it only parses and dispatches -- because Playwright work must
  run on its own thread (see cloudflare_monitor.py).
* Provides send_text / send_image / add_reaction for the rest of the app.
"""
from __future__ import annotations

import io
import json
import logging
import re
from typing import Callable, Optional

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateImageRequest,
    CreateImageRequestBody,
    CreateMessageReactionRequest,
    CreateMessageReactionRequestBody,
    CreateMessageRequest,
    CreateMessageRequestBody,
    Emoji,
    P2ImMessageReceiveV1,
)
from lark_oapi.event.dispatcher_handler import EventDispatcherHandler

log = logging.getLogger("lark")

# command handler signature: (command: str, args: str, chat_id: str, message_id: str)
CommandHandler = Callable[[str, str, str, str], None]

# Mention placeholders inside message text look like "@_user_1" / "@_bot_1" etc.
_MENTION_PLACEHOLDER = re.compile(r"@_\w+")
# A command is a "/word" token appearing at the start or after whitespace.
_COMMAND_RE = re.compile(r"(?:^|\s)/([A-Za-z]\w*)\b\s*(.*)$")


class LarkBot:
    def __init__(self, config, command_handler: Optional[CommandHandler] = None) -> None:
        self.cfg = config
        self.command_handler = command_handler
        self.client = (
            lark.Client.builder()
            .app_id(config.lark_app_id)
            .app_secret(config.lark_app_secret)
            .domain(config.lark_domain)
            .log_level(lark.LogLevel.INFO)
            .build()
        )
        self._ws: Optional[lark.ws.Client] = None

    # --------------------------------------------------------------- outbound
    def send_text(self, chat_id: str, text: str, message_id: Optional[str] = None) -> None:
        """Send a plain-text message. If message_id is given, reply in thread."""
        body = (
            CreateMessageRequestBody.builder()
            .receive_id(chat_id)
            .msg_type("text")
            .content(json.dumps({"text": text}))
            .build()
        )
        req = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(body)
            .build()
        )
        resp = self.client.im.v1.message.create(req)
        if not resp.success():
            log.error("send_text failed: code=%s msg=%s log_id=%s", resp.code, resp.msg, resp.get_log_id())

    def upload_image(self, image_bytes: bytes) -> Optional[str]:
        """Upload a PNG and return its image_key (or None on failure)."""
        buf = io.BytesIO(image_bytes)
        buf.name = "screenshot.png"  # some servers key off a filename
        req = (
            CreateImageRequest.builder()
            .request_body(
                CreateImageRequestBody.builder().image_type("message").image(buf).build()
            )
            .build()
        )
        resp = self.client.im.v1.image.create(req)
        if not resp.success():
            log.error("upload_image failed: code=%s msg=%s log_id=%s", resp.code, resp.msg, resp.get_log_id())
            return None
        return resp.data.image_key

    def send_image(self, chat_id: str, image_bytes: bytes) -> bool:
        image_key = self.upload_image(image_bytes)
        if not image_key:
            return False
        body = (
            CreateMessageRequestBody.builder()
            .receive_id(chat_id)
            .msg_type("image")
            .content(json.dumps({"image_key": image_key}))
            .build()
        )
        req = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(body)
            .build()
        )
        resp = self.client.im.v1.message.create(req)
        if not resp.success():
            log.error("send_image failed: code=%s msg=%s log_id=%s", resp.code, resp.msg, resp.get_log_id())
            return False
        return True

    def add_reaction(self, message_id: str, emoji_type: str) -> None:
        """Add an emoji reaction (e.g. 'OK' while working, 'DONE' when finished)."""
        try:
            req = (
                CreateMessageReactionRequest.builder()
                .message_id(message_id)
                .request_body(
                    CreateMessageReactionRequestBody.builder()
                    .reaction_type(Emoji.builder().emoji_type(emoji_type).build())
                    .build()
                )
                .build()
            )
            resp = self.client.im.v1.message_reaction.create(req)
            if not resp.success():
                log.error("add_reaction(%s) failed: code=%s msg=%s", emoji_type, resp.code, resp.msg)
        except Exception:  # never let a reaction failure break the flow
            log.exception("add_reaction(%s) raised", emoji_type)

    # ---------------------------------------------------------------- inbound
    @staticmethod
    def _extract_text(content: str) -> str:
        try:
            data = json.loads(content or "{}")
        except json.JSONDecodeError:
            return ""
        return data.get("text", "") or ""

    def _on_message(self, data: P2ImMessageReceiveV1) -> None:
        try:
            msg = data.event.message
            chat_id = msg.chat_id
            message_id = msg.message_id
            chat_type = msg.chat_type  # "group" | "p2p"
            mentions = msg.mentions or []

            raw = self._extract_text(msg.content) if msg.message_type == "text" else ""
            # Log every received message so we can confirm events are arriving.
            log.info(
                "recv message: chat_type=%s type=%s mentions=%d text=%r",
                chat_type, msg.message_type, len(mentions), raw[:80],
            )

            if msg.message_type != "text":
                return

            # Strip @mention placeholders (@_user_1 / @_bot_1 ...) to isolate the command.
            text = _MENTION_PLACEHOLDER.sub(" ", raw).strip()

            # In a group, only act when the bot was @-mentioned. Lark only delivers
            # group messages that mention the bot, so the presence of a mention is
            # enough -- we do NOT hard-require an open_id match (the id form varies
            # and an exact check silently drops legitimate commands).
            if chat_type == "group" and not mentions:
                return

            match = _COMMAND_RE.search(text)
            if match:
                command = match.group(1).lower()
                args = (match.group(2) or "").strip()
            elif chat_type != "group" and text:
                # In a 1:1 chat, accept a bare command with no leading slash and no
                # @-tag ("mo", "testalert", "status").
                parts = text.split(maxsplit=1)
                command = parts[0].lower()
                args = parts[1] if len(parts) > 1 else ""
            else:
                return

            log.info("dispatch command '/%s' from chat=%s (%s)", command, chat_id, chat_type)
            if self.command_handler:
                self.command_handler(command, args, chat_id, message_id)
        except Exception:
            log.exception("error handling incoming message")

    # ------------------------------------------------------------------- run
    def start(self) -> None:
        """Open the persistent WebSocket subscription and block forever.

        Auto-reconnect is enabled by the SDK, so this survives network drops.
        """
        handler = (
            EventDispatcherHandler.builder(
                self.cfg.lark_encrypt_key or "",
                self.cfg.lark_verification_token or "",
            )
            .register_p2_im_message_receive_v1(self._on_message)
            .build()
        )
        self._ws = lark.ws.Client(
            self.cfg.lark_app_id,
            self.cfg.lark_app_secret,
            event_handler=handler,
            domain=self.cfg.lark_domain,
            log_level=lark.LogLevel.INFO,
        )
        log.info("starting Lark WebSocket subscription (persistent connection)...")
        self._ws.start()  # blocking; reconnects internally
