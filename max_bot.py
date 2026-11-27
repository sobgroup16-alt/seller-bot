"""Бот Елены Тимошенко для мессенджера MAX.

Та же воронка, что у Telegram-бота: тексты из texts.yaml, база и прогрев через db/funnel.
Точка входа для хостинга — main_max.py. Секреты только из окружения, в код не пишутся.

Официальные отличия, которые здесь учтены:
- старт по ссылке приходит событием bot_started, а не командой /start
  (https://dev.max.ru/docs/chatbots/bots-coding/prepare);
- кнопки — вложение inline_keyboard типов callback и link
  (https://dev.max.ru/docs-api/use-cases/sending-messages/keyboard);
- нажатие нужно подтвердить POST /answers
  (https://dev.max.ru/docs-api/methods/POST/answers);
- текст до 4000 символов, разметка format=html
  (https://dev.max.ru/docs-api/methods/POST/messages).
"""
from __future__ import annotations

import asyncio
import html
import logging
import os
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from zoneinfo import ZoneInfo

from bot import CB_MAIL_NO, CB_MAIL_YES, CB_PD
from broadcast import run_broadcast
from config import Settings, load_settings
from db import AUDIENCES, DB
from funnel import Funnel
from logic import (
    consent_stage,
    landing_link,
    parse_button_line,
    strip_last_line,
    telegram_command,
)
from max_api import (
    MAX_RPS,
    MaxApiError,
    MaxClient,
    callback_button,
    inline_keyboard,
    is_blocked,
    is_retryable,
    link_button,
    sleep_retry,
)
from texts import fmt, load_texts

log = logging.getLogger("max_bot")

UPDATE_TYPES = ["bot_started", "bot_stopped", "message_created", "message_callback"]
TEXT_LIMIT = 4000


def person_from(data: dict | None) -> SimpleNamespace | None:
    if not data or data.get("user_id") in (None, ""):
        return None
    if data.get("is_bot"):
        return None
    first = (data.get("first_name") or data.get("name") or "").strip()
    last = (data.get("last_name") or "").strip()
    full = (data.get("name") or " ".join(part for part in (first, last) if part)).strip()
    return SimpleNamespace(
        id=int(data["user_id"]),
        username=data.get("username") or None,
        full_name=full or None,
        first_name=first or None,
    )


class MaxApp:
    """Сценарий воронки поверх MAX API. Правила согласий те же, что в Telegram-боте."""

    def __init__(self, settings: Settings, texts: dict, db: DB, api: MaxClient):
        self.s = settings
        self.t = texts
        self.db = db
        self.api = api
        self.funnel = Funnel(db, settings, texts)
        self.drafts: dict[int, dict] = {}
        self.broadcast_task: asyncio.Task | None = None

    # ---------- клавиатуры ----------
    def kb_pd(self) -> list:
        return inline_keyboard([[callback_button(self.t["buttons"]["pd_consent"], CB_PD)]])

    def kb_mailing(self) -> list:
        buttons = self.t["buttons"]
        return inline_keyboard([
            [callback_button(buttons["mailing_yes"], CB_MAIL_YES)],
            [callback_button(buttons["mailing_no"], CB_MAIL_NO)],
        ])

    def kb_choice(self) -> list:
        rows = [[callback_button(info["button"], f"lead:{tag}")]
                for tag, info in self.t["sources"].items()]
        return inline_keyboard(rows)

    def kb_url(self, text: str, url: str) -> list | None:
        if not url:
            return None
        return inline_keyboard([[link_button(text, url)]])

    def kb_program(self, source: str | None) -> list | None:
        url = landing_link(self.s.landing_url, "program", source)
        if not url:
            log.warning("Не задан LANDING_URL — кнопка «Посмотреть программу» не показана")
            return None
        return self.kb_url(self.t["buttons"]["program"], url)

    def lead_assets(self, source: str) -> tuple[str, str]:
        if source == "utechki":
            return self.s.lead_utechki_url, self.s.lead_utechki_file_id
        if source == "agent":
            return self.s.lead_agent_url, self.s.lead_agent_file_id
        return "", ""

    # ---------- люди и согласия ----------
    def is_admin(self, user_id: int) -> bool:
        ids = self.s.max_admin_ids or self.s.admin_ids
        return user_id in ids

    @staticmethod
    def name(user: SimpleNamespace) -> str:
        return html.escape(user.first_name or "Здравствуйте")

    def stage_of(self, user_id: int) -> str:
        row = self.db.get_user(user_id)
        if row is None:
            return "pd"
        return consent_stage(row["pd_consent"], row["mailing_consent"])

    def note_user(self, user: SimpleNamespace) -> None:
        self.db.ensure_user(user.id, user.username, user.full_name)

    def requested_source(self, text: str | None = None, callback_data: str | None = None) -> str | None:
        if callback_data and callback_data.startswith("lead:"):
            tag = callback_data.split(":", 1)[1]
            return tag if tag in self.t["sources"] else None
        name, arg = telegram_command(text)
        if name == "/start":
            return self.funnel.source_from_start(arg)
        if name:
            return None
        return self.funnel.source_from_text(text)

    def prepare_incoming(self, user: SimpleNamespace, text: str | None = None,
                         callback_data: str | None = None) -> str:
        stage = self.stage_of(user.id)
        if stage == "done":
            return stage
        self.note_user(user)
        source = self.requested_source(text, callback_data)
        if source:
            self.db.set_pending_source(user.id, source)
        return stage

    def accept_pd(self, user_id: int) -> bool:
        row = self.db.get_user(user_id)
        if row and row["pd_consent"] == 1:
            return False
        self.db.set_pd_consent(user_id)
        self.db.log_event(user_id, "pd_consent")
        log.info("Согласие на обработку ПДн: user_id=%s", user_id)
        return True

    def accept_mailing(self, user_id: int, agreed: bool) -> None:
        self.db.set_mailing_consent(user_id, agreed)
        if not agreed:
            self.db.cancel_promo(user_id)
        self.db.log_event(user_id, "mailing_yes" if agreed else "mailing_no")
        log.info("Решение по рассылке: user_id=%s agreed=%s", user_id, agreed)

    def unsubscribe(self, user_id: int) -> None:
        self.db.set_mailing_consent(user_id, False)
        self.db.cancel_promo(user_id)
        self.db.log_event(user_id, "mailing_stop")
        log.info("Отписка /stop: user_id=%s", user_id)

    def _already_in_funnel(self, user_id: int) -> bool:
        return self.db.has_event(user_id, "start") or self.db.has_lead(user_id)

    async def send(self, user_id: int, text: str, attachments: list | None = None) -> str:
        for _ in range(2):
            try:
                await self.api.send_message(user_id, text, attachments)
                return "ok"
            except MaxApiError as err:
                if is_blocked(err):
                    return "blocked"
                if is_retryable(err):
                    await sleep_retry(err)
                    continue
                log.error("Ошибка отправки → %s: %s", user_id, err)
                return "retry"
        return "retry"

    async def show_pd(self, user_id: int) -> None:
        await self.send(user_id, fmt(self.t["consent"]["pd"]), self.kb_pd())

    async def show_mailing(self, user_id: int) -> None:
        await self.send(user_id, fmt(self.t["consent"]["mailing"]), self.kb_mailing())

    async def deliver_lead(self, user_id: int, user: SimpleNamespace, source: str) -> None:
        self.funnel.register_lead(user.id, user.username, user.full_name, source)
        info = self.t["sources"][source]
        intro = fmt(info["intro"], name=self.name(user))
        url, file_id = self.lead_assets(source)
        # file_id от Telegram в MAX не действует: видео там своё, его нужно загружать отдельно.
        if url:
            await self.send(user_id, intro, self.kb_url(self.t["watch_button"], url))
        else:
            if file_id:
                log.warning(
                    "Для лид-магнита %s задан только Telegram file_id — в MAX его не отправить", source
                )
            else:
                log.warning("Не задана ссылка для лид-магнита %s", source)
            await self.send(user_id, intro + "\n\n" + fmt(self.t["lead_not_ready"]))
        if self.s.program_delay_minutes <= 0:
            await self.send(user_id, fmt(self.t["after_lead"], name=self.name(user)), self.kb_program(source))

    async def finish_and_deliver(self, user_id: int, user: SimpleNamespace) -> None:
        source = self.db.get_pending_source(user.id)
        if source not in self.t["sources"]:
            source = None
        started = self._already_in_funnel(user.id)
        if source or not started:
            self.funnel.register_start(user.id, user.username, user.full_name, source)
        if source:
            await self.deliver_lead(user_id, user, source)
        elif not started:
            await self.send(user_id, fmt(self.t["welcome"]), self.kb_choice())
        self.db.clear_pending_source(user.id)

    async def deliver_start(self, user: SimpleNamespace, payload: str | None) -> None:
        source = self.funnel.source_from_start(payload)
        self.db.clear_pending_source(user.id)
        self.funnel.register_start(user.id, user.username, user.full_name, source)
        if source:
            await self.deliver_lead(user.id, user, source)
        else:
            await self.send(user.id, fmt(self.t["welcome"]), self.kb_choice())

    async def on_stop(self, user: SimpleNamespace) -> None:
        waiting = self.stage_of(user.id) == "mailing"
        self.note_user(user)
        self.unsubscribe(user.id)
        await self.send(user.id, fmt(self.t["consent"]["unsubscribed"]))
        if waiting:
            await self.finish_and_deliver(user.id, user)

    async def on_pd(self, user: SimpleNamespace) -> None:
        self.note_user(user)
        self.accept_pd(user.id)
        if self.stage_of(user.id) == "mailing":
            await self.show_mailing(user.id)
        else:
            await self.finish_and_deliver(user.id, user)

    async def on_mailing(self, user: SimpleNamespace, agreed: bool) -> None:
        self.note_user(user)
        if self.stage_of(user.id) == "pd":
            await self.show_pd(user.id)
            return
        row = self.db.get_user(user.id)
        already = row is not None and row["mailing_consent"] is not None
        pending = row["pending_source"] if row else None
        if already and not pending and self._already_in_funnel(user.id):
            return
        if not already:
            self.accept_mailing(user.id, agreed)
        await self.finish_and_deliver(user.id, user)

    # ---------- отложенные сообщения ----------
    async def send_scheduled(self, user_id: int, step: str, source: str | None) -> str:
        row = self.db.get_user(user_id)
        name = html.escape((row["full_name"] or "").split(" ")[0]) if row and row["full_name"] else "Здравствуйте"
        text = fmt(self.t[step], name=name)
        if step == "channel_invite":
            buttons = self.kb_url(self.t["buttons"]["channel"], self.s.channel_url)
        else:
            buttons = self.kb_program(source)
        return await self.send(user_id, text, buttons)

    async def scheduler_loop(self) -> None:
        log.info("Планировщик MAX запущен (проверка каждые %s с)", self.s.scheduler_interval_seconds)
        while True:
            try:
                n = await self.funnel.process_due(self.send_scheduled)
                if n:
                    log.info("Отправлено отложенных сообщений: %s", n)
            except Exception:  # noqa: BLE001
                log.exception("Ошибка в планировщике MAX")
            await asyncio.sleep(self.s.scheduler_interval_seconds)

    # ---------- события ----------
    async def handle_update(self, update: dict) -> None:
        kind = update.get("update_type")
        if kind == "bot_started":
            await self.on_started(update)
        elif kind == "bot_stopped":
            await self.on_stopped(update)
        elif kind == "message_created":
            await self.on_message(update)
        elif kind == "message_callback":
            await self.on_callback(update)

    async def on_started(self, update: dict) -> None:
        user = person_from(update.get("user"))
        if user is None:
            return
        payload = (update.get("payload") or "").strip() or None
        text = f"/start {payload}" if payload else "/start"
        if self.stage_of(user.id) != "done":
            stage = self.prepare_incoming(user, text=text)
            if stage == "pd":
                await self.show_pd(user.id)
            else:
                await self.show_mailing(user.id)
            return
        await self.deliver_start(user, payload)

    async def on_stopped(self, update: dict) -> None:
        user = person_from(update.get("user"))
        if user is None:
            return
        self.db.mark_blocked(user.id)
        log.info("Пользователь остановил бота в MAX: %s", user.id)

    async def on_message(self, update: dict) -> None:
        message = update.get("message") or {}
        user = person_from((message.get("sender") or {}))
        if user is None:
            return
        body = message.get("body") or {}
        text = body.get("text") or ""
        name, arg = telegram_command(text)
        if name == "/stop":
            await self.on_stop(user)
            return
        stage = self.prepare_incoming(user, text=text or None)
        if stage == "pd":
            await self.show_pd(user.id)
            return
        if stage == "mailing":
            await self.show_mailing(user.id)
            return
        if name == "/start":
            await self.deliver_start(user, arg)
            return
        if name == "/myid":
            await self.send(user.id, fmt(self.t["myid"], chat_id=user.id))
            return
        if name == "/stats":
            await self.on_stats(user)
            return
        if name == "/broadcast":
            await self.on_broadcast(user)
            return
        if name == "/cancel":
            await self.on_cancel(user)
            return
        if self.waiting_draft(user.id):
            await self.take_draft(user, body)
            return
        if not text.strip():
            return
        source = self.funnel.source_from_text(text)
        if source:
            self.funnel.register_start(user.id, user.username, user.full_name, source)
            await self.deliver_lead(user.id, user, source)
        else:
            await self.send(user.id, fmt(self.t["unknown"]), self.kb_choice())

    async def on_callback(self, update: dict) -> None:
        callback = update.get("callback") or {}
        user = person_from(callback.get("user"))
        payload = callback.get("payload") or ""
        callback_id = callback.get("callback_id")
        if callback_id:
            try:
                await self.api.answer_callback(str(callback_id))
            except MaxApiError as err:
                log.warning("Не удалось ответить на callback %s: %s", callback_id, err)
        if user is None:
            return
        if payload == CB_PD:
            await self.on_pd(user)
            return
        if payload in (CB_MAIL_YES, CB_MAIL_NO):
            await self.on_mailing(user, payload == CB_MAIL_YES)
            return
        stage = self.prepare_incoming(user, callback_data=payload)
        if stage == "pd":
            await self.show_pd(user.id)
            return
        if stage == "mailing":
            await self.show_mailing(user.id)
            return
        if payload.startswith("lead:"):
            source = payload.split(":", 1)[1]
            if source in self.t["sources"]:
                await self.deliver_lead(user.id, user, source)
            return
        if payload.startswith("bc:"):
            await self.on_broadcast_button(user, payload)

    # ---------- рассылка (только админ MAX) ----------
    def broadcast_running(self) -> bool:
        return self.broadcast_task is not None and not self.broadcast_task.done()

    def waiting_draft(self, user_id: int) -> bool:
        return self.is_admin(user_id) and self.drafts.get(user_id, {}).get("stage") == "wait"

    def audience_label(self, audience: str) -> str:
        return self.t["admin"]["bc_audiences"][audience]

    async def on_stats(self, user: SimpleNamespace) -> None:
        if not self.is_admin(user.id):
            await self.send(user.id, fmt(self.t["admin"]["not_admin"]))
            return
        await self.send(user.id, self.stats_text())

    async def on_broadcast(self, user: SimpleNamespace) -> None:
        if not self.is_admin(user.id):
            await self.send(user.id, fmt(self.t["admin"]["not_admin"]))
            return
        if self.broadcast_running():
            await self.send(user.id, fmt(self.t["admin"]["bc_busy"]))
            return
        self.drafts[user.id] = {"stage": "wait"}
        await self.send(user.id, fmt(self.t["admin"]["bc_ask"]))

    async def on_cancel(self, user: SimpleNamespace) -> None:
        if self.is_admin(user.id) and self.drafts.pop(user.id, None):
            await self.send(user.id, fmt(self.t["admin"]["bc_cancelled"]))

    def draft_from_body(self, body: dict) -> tuple[dict | None, str | None]:
        text = body.get("text") or ""
        attachment = _media_attachment(body.get("attachments"))
        button, has_button = parse_button_line(text)
        if has_button and not button:
            return None, "bc_button_bad"
        plain = strip_last_line(text) if has_button else text
        if not plain.strip() and attachment is None:
            return None, "bc_empty"
        if len(plain) > TEXT_LIMIT:
            return None, "bc_too_long"
        kind = "text"
        token = None
        if attachment:
            kind = attachment["type"] if attachment["type"] in {"image", "video"} else "file"
            token = (attachment.get("payload") or {}).get("token")
        return {
            "stage": "ready",
            "kind": kind,
            "token": token,
            "html": html.escape(plain),
            "button": button,
            "audience": "all",
        }, None

    def draft_attachments(self, draft: dict) -> list | None:
        rows = []
        if draft.get("button"):
            label, url = draft["button"]
            rows.append([link_button(label, url)])
        attachments = []
        if draft.get("token") and draft["kind"] in {"image", "video", "file"}:
            attachments.append({"type": draft["kind"], "payload": {"token": draft["token"]}})
        if rows:
            attachments.extend(inline_keyboard(rows))
        return attachments or None

    async def send_draft(self, user_id: int, draft: dict) -> None:
        await self.api.send_message(user_id, draft["html"] or " ", self.draft_attachments(draft))

    def bc_control(self, draft: dict) -> tuple[str, list]:
        counts = {aud: len(self.db.broadcast_recipients(aud)) for aud in AUDIENCES}
        aud = draft["audience"]
        text = fmt(self.t["admin"]["bc_preview"], audience=self.audience_label(aud), count=counts[aud])
        buttons = self.t["buttons"]
        row = [callback_button(
            ("✅ " if item == aud else "") + f"{self.audience_label(item)} ({counts[item]})",
            f"bc:aud:{item}",
        ) for item in AUDIENCES]
        keyboard = inline_keyboard([
            row,
            [callback_button(buttons["bc_send"], "bc:send"),
             callback_button(buttons["bc_cancel"], "bc:cancel")],
        ])
        return text, keyboard

    async def take_draft(self, user: SimpleNamespace, body: dict) -> None:
        draft, err = self.draft_from_body(body)
        if err:
            await self.send(user.id, fmt(self.t["admin"][err]))
            return
        self.drafts[user.id] = draft
        await self.send(user.id, fmt(self.t["admin"]["bc_preview_title"]))
        await self.send_draft(user.id, draft)
        text, keyboard = self.bc_control(draft)
        await self.send(user.id, text, keyboard)

    async def on_broadcast_button(self, user: SimpleNamespace, payload: str) -> None:
        if not self.is_admin(user.id):
            await self.send(user.id, fmt(self.t["admin"]["not_admin"]))
            return
        draft = self.drafts.get(user.id)
        if not draft or draft.get("stage") != "ready":
            await self.send(user.id, fmt(self.t["admin"]["bc_no_draft"]))
            return
        parts = payload.split(":")
        if len(parts) == 3 and parts[1] == "aud" and parts[2] in AUDIENCES:
            draft["audience"] = parts[2]
            text, keyboard = self.bc_control(draft)
            await self.send(user.id, text, keyboard)
        elif payload == "bc:cancel":
            self.drafts.pop(user.id, None)
            await self.send(user.id, fmt(self.t["admin"]["bc_cancelled"]))
        elif payload == "bc:send":
            if self.broadcast_running():
                await self.send(user.id, fmt(self.t["admin"]["bc_busy"]))
                return
            self.drafts.pop(user.id, None)
            total = await self.start_broadcast(user.id, draft)
            rate = min(self.s.broadcast_rate_per_sec, MAX_RPS)
            await self.send(user.id, fmt(
                self.t["admin"]["bc_started"], total=total, rate=f"{rate:g}",
                audience=self.audience_label(draft["audience"]),
            ))

    async def broadcast_send_one(self, draft: dict, user_id: int):
        try:
            await self.send_draft(user_id, draft)
            return "ok"
        except MaxApiError as err:
            if is_retryable(err):
                return ("retry", err.retry_after or 1)
            if is_blocked(err):
                return "blocked"
            log.error("Рассылка MAX → %s: %s", user_id, err)
            return "failed"

    async def start_broadcast(self, admin_id: int, draft: dict) -> int:
        recipients = self.db.broadcast_recipients(draft["audience"])
        bc_id = self.db.create_broadcast(admin_id, draft["audience"], draft["kind"], draft["html"], len(recipients))
        self.broadcast_task = asyncio.create_task(self._broadcast_job(admin_id, bc_id, draft, recipients))
        return len(recipients)

    async def _broadcast_job(self, admin_id: int, bc_id: int, draft: dict, recipients: list[int]) -> None:
        status = "done"
        res = None
        try:
            res = await run_broadcast(
                recipients,
                lambda uid: self.broadcast_send_one(draft, uid),
                rate_per_sec=min(self.s.broadcast_rate_per_sec, MAX_RPS),
                on_blocked=self.db.mark_blocked,
            )
        except Exception:  # noqa: BLE001
            log.exception("Рассылка MAX %s прервалась", bc_id)
            status = "error"
        sent, failed, blocked = (res.sent, res.failed, res.blocked) if res else (0, 0, 0)
        self.db.finish_broadcast(bc_id, sent, failed, blocked, status)
        try:
            await self.send(admin_id, fmt(
                self.t["admin"]["bc_done"], total=len(recipients), sent=sent, failed=failed, blocked=blocked,
            ))
        except Exception:  # noqa: BLE001
            log.exception("Не удалось отправить итог рассылки MAX")

    def stats_text(self) -> str:
        st = self.db.stats()
        title = self.funnel.source_title
        lines = ["📊 <b>Статистика бота</b>", "", f"Всего людей: <b>{st['users_total']}</b>",
                 "Пришли первым словом:"]
        for row in st["by_source"]:
            label = title(row["s"]) if row["s"] != "без слова" else "без кодового слова"
            lines.append(f"• {html.escape(label)}: {row['n']}")
        lines.append("Получили видео:")
        for tag in self.t["sources"]:
            lines.append(f"• {html.escape(title(tag))}: {st['leads'].get(tag, 0)}")
        lines.append("")
        lines.append("Переходы на программу курса бот не видит: смотрите их в аналитике сайта по метке src.")
        lines.append("")
        lines.append(
            "Отложенные сообщения отправлены: "
            f"программа курса — {st['sent'].get('after_lead', 0)}, "
            f"приглашение в канал — {st['sent'].get('channel_invite', 0)}, "
            f"прогрев 1 — {st['sent'].get('warmup1', 0)}, "
            f"прогрев 2 — {st['sent'].get('warmup2', 0)}"
        )
        lines.append(f"Остановили бота: {st['blocked']}")
        lines.append(f"Рассылок всего: {st['broadcasts']}")
        last = st["broadcast_last"]
        if last:
            when = datetime.fromtimestamp(last["created_at"], ZoneInfo(self.s.timezone)).strftime("%d.%m %H:%M")
            state = "идёт" if last["status"] == "running" else (
                f"доставлено {last['sent']} из {last['total']}, ошибок {last['failed']}, "
                f"остановили бота {last['blocked']}")
            lines.append(f"Последняя: {when} ({self.s.timezone}), {self.audience_label(last['audience'])} — {state}")
        return "\n".join(lines)

    # ---------- long polling ----------
    async def prepare_updates(self) -> None:
        """Снимает чужие Webhook, иначе long polling молчит. MAX_KEEP_WEBHOOKS=1 оставляет их."""
        if _keep_webhooks():
            log.info("MAX_KEEP_WEBHOOKS=1: существующие Webhook не удаляются")
            await self._warn_if_webhook()
            return
        try:
            subs = await self.api.get_subscriptions()
        except Exception as err:  # noqa: BLE001
            log.warning("Не удалось получить подписки Webhook, продолжаю: %s", err)
            return
        for url in _subscription_urls(subs):
            try:
                await self.api.delete_subscription(url)
            except Exception as err:  # noqa: BLE001
                log.warning("Не удалось удалить Webhook %s, продолжаю: %s", url, err)
                continue
            log.info("Удалена подписка Webhook: %s", url)
        try:
            left = await self.api.get_subscriptions()
        except Exception as err:  # noqa: BLE001
            log.warning("Не удалось проверить список подписок после удаления: %s", err)
            return
        rest = _subscription_urls(left)
        if rest:
            log.warning("Подписки Webhook остались, long polling может не получать события: %s", ", ".join(rest))
        else:
            log.info("Подписок Webhook нет")

    async def _warn_if_webhook(self) -> None:
        try:
            subs = await self.api.get_subscriptions()
        except Exception as err:  # noqa: BLE001
            log.warning("Не удалось проверить подписки Webhook: %s", err)
            return
        urls = _subscription_urls(subs)
        if urls:
            log.warning(
                "У бота есть Webhook (%s). Пока подписка активна, long polling события не получает.",
                ", ".join(urls),
            )

    async def poll_forever(self, marker_path: Path) -> None:
        marker = _read_marker(marker_path)
        await self.prepare_updates()
        while True:
            try:
                page = await self.api.get_updates(marker=marker, timeout=25, types=UPDATE_TYPES)
            except MaxApiError as err:
                if err.status == 401:
                    log.error("MAX отклонил токен (401). Проверьте MAX_BOT_TOKEN")
                    raise
                log.warning("Long polling: %s", err)
                await asyncio.sleep(3)
                continue
            except Exception:  # noqa: BLE001
                log.exception("Long polling оборвался")
                await asyncio.sleep(3)
                continue
            for update in page.get("updates") or []:
                try:
                    await self.handle_update(update)
                except Exception:  # noqa: BLE001
                    log.exception("Не обработано событие %s", update.get("update_type"))
            if page.get("marker") is not None:
                marker = page["marker"]
                _write_marker(marker_path, marker)


def _keep_webhooks() -> bool:
    return os.getenv("MAX_KEEP_WEBHOOKS", "").strip().lower() in {"1", "true", "yes", "on", "да"}


def _subscription_urls(subs: list) -> list[str]:
    urls = []
    for item in subs or []:
        if isinstance(item, dict) and item.get("url"):
            urls.append(str(item["url"]))
        elif isinstance(item, str) and item:
            urls.append(item)
    return urls


def _media_attachment(attachments: list | None) -> dict | None:
    for item in attachments or []:
        if isinstance(item, dict) and item.get("type") in {"image", "video", "file"}:
            return item
    return None


def _read_marker(path: Path) -> int | None:
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _write_marker(path: Path, marker: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(marker), encoding="utf-8")


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings(require_token=False)
    if not settings.max_bot_token:
        raise SystemExit(
            "Не задан MAX_BOT_TOKEN. Скопируйте токен в платформе MAX для партнёров: "
            "Чат-боты → ⋮ → Настройки. Документация: https://dev.max.ru/docs/chatbots/bots-create/manage"
        )
    texts = load_texts()
    db_path = settings.max_db_path or (Path(__file__).resolve().parent / "data" / "max_bot.sqlite3")
    db = DB(db_path)
    log.info("База MAX: %s", db_path)
    api = MaxClient(settings.max_bot_token)
    app = MaxApp(settings, texts, db, api)
    scheduler: asyncio.Task | None = None
    try:
        try:
            me = await api.get_me()
        except MaxApiError as err:
            raise SystemExit(f"MAX не принял токен: {err}") from err
        username = me.get("username") or me.get("name") or ""
        if username:
            log.info(
                "Бот MAX @%s запущен. Ссылки: https://max.ru/%s?start=utechki и https://max.ru/%s?start=agent",
                username, username, username,
            )
        else:
            log.info("Бот MAX запущен (ник в ответе /me не пришёл)")
        if not settings.landing_url:
            log.warning("LANDING_URL не задан: кнопка «Посмотреть программу» не показывается")
        marker_path = Path(str(db_path) + ".marker")
        scheduler = asyncio.create_task(app.scheduler_loop())
        await app.poll_forever(marker_path)
    finally:
        if scheduler is not None:
            scheduler.cancel()
            try:
                await scheduler
            except asyncio.CancelledError:
                pass
        await api.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit) as exc:
        if isinstance(exc, SystemExit) and exc.code not in (None, 0):
            raise
