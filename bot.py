"""Telegram-бот Елены Тимошенко: лид-магниты по кодовым словам, ссылка на программу курса,
приглашение в канал, прогрев и рассылка по базе.

Перед материалом бот спрашивает согласие на обработку персональных данных и на рассылку.
Прогрев и рассылку получают только те, кто нажал «Да, присылайте».

Оплаты в боте нет: курс оплачивается на лендинге (Prodamus), бот оплату не отслеживает.

Запуск: python main.py   (или ./run.sh — с автоперезапуском и логом)
"""
from __future__ import annotations

import asyncio
import html
import logging
from datetime import datetime

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
    User,
)
from zoneinfo import ZoneInfo

from broadcast import run_broadcast
from config import Settings, load_settings
from db import AUDIENCES, DB
from funnel import Funnel
from logic import consent_stage, landing_link, parse_button_line, strip_last_line, telegram_command
from texts import fmt, load_texts

log = logging.getLogger("bot")

# Ошибки Telegram, после которых человеку больше не пишем
GONE_MARKERS = ("chat not found", "user is deactivated", "bot was blocked", "user not found",
                "peer_id_invalid", "bot can't initiate conversation")

CB_PD = "consent:pd"
CB_MAIL_YES = "consent:mail:yes"
CB_MAIL_NO = "consent:mail:no"


class App:
    """Всё, что нужно обработчикам: настройки, тексты, база, воронка, бот."""

    def __init__(self, settings: Settings, texts: dict, db: DB, bot: Bot):
        self.s = settings
        self.t = texts
        self.db = db
        self.bot = bot
        self.funnel = Funnel(db, settings, texts)
        self.drafts: dict[int, dict] = {}          # черновики рассылки по id админа
        self.broadcast_task: asyncio.Task | None = None

    # ---------- клавиатуры ----------
    def kb_choice(self) -> InlineKeyboardMarkup:
        rows = [[InlineKeyboardButton(text=info["button"], callback_data=f"lead:{tag}")]
                for tag, info in self.t["sources"].items()]
        return InlineKeyboardMarkup(inline_keyboard=rows)

    def program_url(self, source: str | None) -> str:
        return landing_link(self.s.landing_url, "program", source)

    def kb_program(self, source: str | None) -> InlineKeyboardMarkup | None:
        """Одна кнопка «Посмотреть программу» → лендинг, блок программы (#program)."""
        url = self.program_url(source)
        if not url:
            log.warning("Не задан LANDING_URL — кнопка «Посмотреть программу» не показана")
            return None
        return self.kb_url(self.t["buttons"]["program"], url)

    @staticmethod
    def kb_url(text: str, url: str) -> InlineKeyboardMarkup | None:
        if not url:
            return None
        return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=text, url=url)]])

    # ---------- помощники ----------
    def is_admin(self, user_id: int) -> bool:
        return user_id in self.s.admin_ids

    @staticmethod
    def name(user: User) -> str:
        return html.escape(user.first_name or "Здравствуйте")

    # ---------- согласия ----------
    def kb_pd(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=self.t["buttons"]["pd_consent"], callback_data=CB_PD),
        ]])

    def kb_mailing(self) -> InlineKeyboardMarkup:
        buttons = self.t["buttons"]
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=buttons["mailing_yes"], callback_data=CB_MAIL_YES)],
            [InlineKeyboardButton(text=buttons["mailing_no"], callback_data=CB_MAIL_NO)],
        ])

    def stage_of(self, user_id: int) -> str:
        row = self.db.get_user(user_id)
        if row is None:
            return "pd"
        return consent_stage(row["pd_consent"], row["mailing_consent"])

    def note_user(self, user: User) -> None:
        self.db.ensure_user(user.id, user.username, user.full_name)

    def requested_source(self, text: str | None = None, callback_data: str | None = None) -> str | None:
        """Какой материал просили, пока согласия не закончены. None — не затирать уже сохранённое."""
        if callback_data and callback_data.startswith("lead:"):
            tag = callback_data.split(":", 1)[1]
            return tag if tag in self.t["sources"] else None
        name, arg = telegram_command(text)
        if name == "/start":
            return self.funnel.source_from_start(arg)
        if name:
            return None
        return self.funnel.source_from_text(text)

    def prepare_incoming(self, user: User, text: str | None = None,
                         callback_data: str | None = None) -> str:
        """Запоминает человека и параметр запуска, если согласия ещё не пройдены."""
        stage = self.stage_of(user.id)
        if stage == "done":
            return stage
        self.note_user(user)
        source = self.requested_source(text, callback_data)
        if source:
            self.db.set_pending_source(user.id, source)
        return stage

    async def show_pd(self, chat_id: int) -> None:
        await self.bot.send_message(chat_id, fmt(self.t["consent"]["pd"]), reply_markup=self.kb_pd())

    async def show_mailing(self, chat_id: int) -> None:
        await self.bot.send_message(chat_id, fmt(self.t["consent"]["mailing"]), reply_markup=self.kb_mailing())

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

    async def finish_and_deliver(self, chat_id: int, user: User) -> None:
        """После согласий выдаёт сохранённый материал. Без параметра новому человеку — меню /start.

        Кто уже был в боте и новый материал не просил, повторное меню не получает.
        Параметр запуска стирается только после успешной выдачи.
        """
        source = self.db.get_pending_source(user.id)
        if source not in self.t["sources"]:
            source = None
        started = self._already_in_funnel(user.id)
        if source or not started:
            self.funnel.register_start(user.id, user.username, user.full_name, source)
        if source:
            await self.deliver_lead(chat_id, user, source)
        elif not started:
            await self.bot.send_message(chat_id, fmt(self.t["welcome"]), reply_markup=self.kb_choice())
        self.db.clear_pending_source(user.id)

    def lead_assets(self, source: str) -> tuple[str, str]:
        if source == "utechki":
            return self.s.lead_utechki_url, self.s.lead_utechki_file_id
        if source == "agent":
            return self.s.lead_agent_url, self.s.lead_agent_file_id
        return "", ""

    # ---------- выдача лид-магнита ----------
    async def deliver_lead(self, chat_id: int, user: User, source: str) -> None:
        self.funnel.register_lead(user.id, user.username, user.full_name, source)
        info = self.t["sources"][source]
        intro = fmt(info["intro"], name=self.name(user))
        url, file_id = self.lead_assets(source)
        if file_id:
            await self.bot.send_video(chat_id, file_id, caption=intro[:1024])
        elif url:
            await self.bot.send_message(chat_id, intro, reply_markup=self.kb_url(self.t["watch_button"], url))
        else:
            log.warning("Не задана ссылка или file_id для лид-магнита %s", source)
            await self.bot.send_message(chat_id, intro + "\n\n" + fmt(self.t["lead_not_ready"]))
        # Сообщение с программой: сразу, если PROGRAM_DELAY_MINUTES=0, иначе его отправит планировщик.
        if self.s.program_delay_minutes <= 0:
            await self.bot.send_message(chat_id, fmt(self.t["after_lead"], name=self.name(user)),
                                        reply_markup=self.kb_program(source))

    # ---------- отложенные сообщения ----------
    async def send_scheduled(self, user_id: int, step: str, source: str | None) -> str:
        row = self.db.get_user(user_id)
        name = html.escape((row["full_name"] or "").split(" ")[0]) if row and row["full_name"] else "Здравствуйте"
        text = fmt(self.t[step], name=name)
        if step == "channel_invite":
            kb = self.kb_url(self.t["buttons"]["channel"], self.s.channel_url)
        else:
            kb = self.kb_program(source)
        try:
            await self.bot.send_message(user_id, text, reply_markup=kb)
            return "ok"
        except TelegramForbiddenError:
            return "blocked"  # человек остановил бота
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
            return "retry"
        except TelegramBadRequest as e:
            if any(m in str(e).lower() for m in GONE_MARKERS):
                return "blocked"
            log.error("Ошибка отправки %s → %s: %s", step, user_id, e)
            return "retry"

    async def scheduler_loop(self) -> None:
        log.info("Планировщик сообщений запущен (проверка каждые %s с)", self.s.scheduler_interval_seconds)
        while True:
            try:
                n = await self.funnel.process_due(self.send_scheduled)
                if n:
                    log.info("Отправлено отложенных сообщений: %s", n)
            except Exception:  # noqa: BLE001
                log.exception("Ошибка в планировщике")
            await asyncio.sleep(self.s.scheduler_interval_seconds)

    # ---------- рассылка ----------
    def broadcast_running(self) -> bool:
        return self.broadcast_task is not None and not self.broadcast_task.done()

    def waiting_draft(self, user_id: int) -> bool:
        return self.is_admin(user_id) and self.drafts.get(user_id, {}).get("stage") == "wait"

    def draft_from_message(self, message: Message) -> tuple[dict | None, str | None]:
        """Собирает черновик из сообщения Елены. Возвращает (черновик, ключ ошибки в texts.admin)."""
        if message.photo:
            kind, file_id = "photo", message.photo[-1].file_id
        elif message.video:
            kind, file_id = "video", message.video.file_id
        elif message.text:
            kind, file_id = "text", None
        else:
            return None, "bc_unsupported"
        plain = (message.caption if kind != "text" else message.text) or ""
        body_html = message.html_text or ""   # с жирным, курсивом и ссылками, как написала Елена
        button, has_button_line = parse_button_line(plain)
        if has_button_line and not button:
            return None, "bc_button_bad"
        if has_button_line:
            body_html = strip_last_line(body_html)
        if kind == "text" and not body_html.strip():
            return None, "bc_empty"
        limit = 4096 if kind == "text" else 1024
        if len(body_html) > limit:
            return None, "bc_too_long"
        return {"stage": "ready", "kind": kind, "file_id": file_id, "html": body_html,
                "button": button, "audience": "all"}, None

    @staticmethod
    def draft_markup(draft: dict) -> InlineKeyboardMarkup | None:
        if not draft.get("button"):
            return None
        label, url = draft["button"]
        return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=label, url=url)]])

    async def send_draft(self, chat_id: int, draft: dict) -> None:
        kb = self.draft_markup(draft)
        if draft["kind"] == "photo":
            await self.bot.send_photo(chat_id, draft["file_id"], caption=draft["html"] or None, reply_markup=kb)
        elif draft["kind"] == "video":
            await self.bot.send_video(chat_id, draft["file_id"], caption=draft["html"] or None, reply_markup=kb)
        else:
            await self.bot.send_message(chat_id, draft["html"], reply_markup=kb)

    async def broadcast_send_one(self, draft: dict, user_id: int):
        try:
            await self.send_draft(user_id, draft)
            return "ok"
        except TelegramRetryAfter as e:
            return ("retry", e.retry_after)
        except TelegramForbiddenError:
            return "blocked"
        except TelegramBadRequest as e:
            if any(m in str(e).lower() for m in GONE_MARKERS):
                return "blocked"
            log.error("Рассылка → %s: %s", user_id, e)
            return "failed"

    def audience_label(self, audience: str) -> str:
        return self.t["admin"]["bc_audiences"][audience]

    def bc_control(self, draft: dict) -> tuple[str, InlineKeyboardMarkup]:
        counts = {a: len(self.db.broadcast_recipients(a)) for a in AUDIENCES}
        aud = draft["audience"]
        text = fmt(self.t["admin"]["bc_preview"], audience=self.audience_label(aud), count=counts[aud])
        b = self.t["buttons"]
        row = [InlineKeyboardButton(text=("✅ " if a == aud else "") + f"{self.audience_label(a)} ({counts[a]})",
                                    callback_data=f"bc:aud:{a}") for a in AUDIENCES]
        kb = InlineKeyboardMarkup(inline_keyboard=[
            row,
            [InlineKeyboardButton(text=b["bc_send"], callback_data="bc:send"),
             InlineKeyboardButton(text=b["bc_cancel"], callback_data="bc:cancel")],
        ])
        return text, kb

    async def start_broadcast(self, admin_id: int, draft: dict) -> int:
        recipients = self.db.broadcast_recipients(draft["audience"])
        bc_id = self.db.create_broadcast(admin_id, draft["audience"], draft["kind"], draft["html"], len(recipients))
        self.broadcast_task = asyncio.create_task(self._broadcast_job(admin_id, bc_id, draft, recipients))
        return len(recipients)

    async def _broadcast_job(self, admin_id: int, bc_id: int, draft: dict, recipients: list[int]) -> None:
        status = "done"
        res = None
        try:
            res = await run_broadcast(recipients, lambda uid: self.broadcast_send_one(draft, uid),
                                      rate_per_sec=self.s.broadcast_rate_per_sec,
                                      on_blocked=self.db.mark_blocked)
        except Exception:  # noqa: BLE001
            log.exception("Рассылка %s прервалась", bc_id)
            status = "error"
        sent, failed, blocked = (res.sent, res.failed, res.blocked) if res else (0, 0, 0)
        self.db.finish_broadcast(bc_id, sent, failed, blocked, status)
        log.info("Рассылка %s: отправлено %s, ошибок %s, остановили бота %s", bc_id, sent, failed, blocked)
        try:
            await self.bot.send_message(admin_id, fmt(self.t["admin"]["bc_done"], total=len(recipients),
                                                      sent=sent, failed=failed, blocked=blocked))
        except Exception:  # noqa: BLE001
            log.exception("Не удалось отправить итог рассылки")

    # ---------- статистика ----------
    def stats_text(self) -> str:
        st = self.db.stats()
        title = self.funnel.source_title
        lines = ["📊 <b>Статистика бота</b>", "", f"Всего людей: <b>{st['users_total']}</b>",
                 "Пришли первым словом:"]
        for r in st["by_source"]:
            label = title(r["s"]) if r["s"] != "без слова" else "без кодового слова"
            lines.append(f"• {html.escape(label)}: {r['n']}")
        lines.append("Получили видео:")
        for tag in self.t["sources"]:
            lines.append(f"• {html.escape(title(tag))}: {st['leads'].get(tag, 0)}")
        lines.append("")
        lines.append("Переходы на программу курса бот не видит: смотрите их в аналитике сайта по метке src.")
        lines.append("")
        lines.append("Отложенные сообщения отправлены: "
                     f"программа курса — {st['sent'].get('after_lead', 0)}, "
                     f"приглашение в канал — {st['sent'].get('channel_invite', 0)}, "
                     f"прогрев 1 — {st['sent'].get('warmup1', 0)}, "
                     f"прогрев 2 — {st['sent'].get('warmup2', 0)}")
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


def _callback_chat_id(cb: CallbackQuery) -> int:
    message = cb.message
    if message is not None and getattr(message, "chat", None) is not None:
        return message.chat.id
    return cb.from_user.id


async def _ack(cb: CallbackQuery) -> None:
    try:
        await cb.answer()
    except RuntimeError:
        log.debug("Не удалось ответить на callback: нет контекста бота")


async def _drop_markup(cb: CallbackQuery) -> None:
    message = cb.message
    if message is None or not hasattr(message, "edit_reply_markup"):
        return
    try:
        await message.edit_reply_markup(reply_markup=None)
    except (TelegramBadRequest, RuntimeError):
        pass


class ConsentMiddleware(BaseMiddleware):
    """До согласия на ПДн любое сообщение и любая команда снова показывают шаг 1.

    /stop проходит всегда: отписка не ждёт анкету.
    Шаг про рассылку тоже обязателен, материал уходит только после ответа на него.
    """

    def __init__(self, app: App):
        self.app = app

    async def __call__(self, handler, event: TelegramObject, data: dict):
        if isinstance(event, Message):
            if event.from_user is None:
                return await handler(event, data)
            name, _arg = telegram_command(event.text)
            if name == "/stop":
                return await handler(event, data)
            text = event.text if event.text is not None else event.caption
            stage = self.app.prepare_incoming(event.from_user, text=text)
            if stage == "done":
                return await handler(event, data)
            if stage == "pd":
                await self.app.show_pd(event.chat.id)
            else:
                await self.app.show_mailing(event.chat.id)
            return None
        if isinstance(event, CallbackQuery):
            if (event.data or "").startswith("consent:"):
                return await handler(event, data)
            if event.from_user is None:
                return await handler(event, data)
            stage = self.app.prepare_incoming(event.from_user, callback_data=event.data)
            if stage == "done":
                return await handler(event, data)
            await _ack(event)
            chat_id = _callback_chat_id(event)
            if stage == "pd":
                await self.app.show_pd(chat_id)
            else:
                await self.app.show_mailing(chat_id)
            return None
        return await handler(event, data)


def build_router(app: App) -> Router:
    r = Router()
    t = app.t
    a = t["admin"]

    @r.message(CommandStart())
    async def on_start(message: Message, command: CommandObject):
        # Сюда попадаем только после обоих согласий: до этого экран согласий перехватывает /start.
        user = message.from_user
        source = app.funnel.source_from_start(command.args)
        app.db.clear_pending_source(user.id)
        app.funnel.register_start(user.id, user.username, user.full_name, source)
        if source:
            await app.deliver_lead(message.chat.id, user, source)
        else:
            await message.answer(fmt(t["welcome"]), reply_markup=app.kb_choice())

    @r.message(Command("stop"))
    async def on_stop(message: Message):
        user = message.from_user
        if user is None:
            return
        # На шаге 2 /stop — это отказ от рассылки, материал после него всё равно выдаём.
        waiting_material = app.stage_of(user.id) == "mailing"
        app.note_user(user)
        app.unsubscribe(user.id)
        await message.answer(fmt(t["consent"]["unsubscribed"]))
        if waiting_material:
            await app.finish_and_deliver(message.chat.id, user)

    @r.message(Command("myid"))
    async def on_myid(message: Message):
        await message.answer(fmt(t["myid"], chat_id=message.chat.id))

    @r.message(Command("stats"))
    async def on_stats(message: Message):
        if not app.is_admin(message.from_user.id):
            await message.answer(fmt(a["not_admin"]))
            return
        await message.answer(app.stats_text())

    # ----- рассылка (только админ) -----
    @r.message(Command("broadcast"))
    async def on_broadcast(message: Message):
        uid = message.from_user.id
        if not app.is_admin(uid):
            await message.answer(fmt(a["not_admin"]))
            return
        if app.broadcast_running():
            await message.answer(fmt(a["bc_busy"]))
            return
        app.drafts[uid] = {"stage": "wait"}
        await message.answer(fmt(a["bc_ask"]))

    @r.message(Command("cancel"))
    async def on_cancel(message: Message):
        if app.is_admin(message.from_user.id) and app.drafts.pop(message.from_user.id, None):
            await message.answer(fmt(a["bc_cancelled"]))

    @r.message(lambda m: m.from_user is not None and app.waiting_draft(m.from_user.id))
    async def on_broadcast_draft(message: Message):
        uid = message.from_user.id
        draft, err = app.draft_from_message(message)
        if err:
            await message.answer(fmt(a[err]))
            return
        app.drafts[uid] = draft
        await message.answer(fmt(a["bc_preview_title"]))
        await app.send_draft(message.chat.id, draft)
        text, kb = app.bc_control(draft)
        await message.answer(text, reply_markup=kb)

    @r.callback_query(F.data.startswith("bc:"))
    async def on_broadcast_button(cb: CallbackQuery):
        uid = cb.from_user.id
        if not app.is_admin(uid):
            await cb.answer(fmt(a["not_admin"]), show_alert=True)
            return
        draft = app.drafts.get(uid)
        if not draft or draft.get("stage") != "ready":
            await cb.answer(fmt(a["bc_no_draft"]), show_alert=True)
            return
        action = cb.data.split(":")
        if action[1] == "aud" and len(action) == 3 and action[2] in AUDIENCES:
            draft["audience"] = action[2]
            text, kb = app.bc_control(draft)
            await cb.answer()
            try:
                await cb.message.edit_text(text, reply_markup=kb)
            except TelegramBadRequest:
                pass  # «message is not modified» — ничего страшного
        elif action[1] == "cancel":
            app.drafts.pop(uid, None)
            await cb.answer()
            await cb.message.edit_text(fmt(a["bc_cancelled"]))
        elif action[1] == "send":
            if app.broadcast_running():
                await cb.answer(fmt(a["bc_busy"]), show_alert=True)
                return
            app.drafts.pop(uid, None)
            await cb.answer()
            total = await app.start_broadcast(uid, draft)
            rate = app.s.broadcast_rate_per_sec
            await cb.message.edit_text(fmt(a["bc_started"], total=total, rate=f"{rate:g}",
                                           audience=app.audience_label(draft["audience"])))
        else:
            await cb.answer()

    # ----- согласия -----
    @r.callback_query(F.data == CB_PD)
    async def on_pd_consent(cb: CallbackQuery):
        user = cb.from_user
        app.note_user(user)
        app.accept_pd(user.id)
        await _ack(cb)
        await _drop_markup(cb)
        chat_id = _callback_chat_id(cb)
        if app.stage_of(user.id) == "mailing":
            await app.show_mailing(chat_id)
        else:
            await app.finish_and_deliver(chat_id, user)

    @r.callback_query(F.data.in_((CB_MAIL_YES, CB_MAIL_NO)))
    async def on_mailing_choice(cb: CallbackQuery):
        user = cb.from_user
        app.note_user(user)
        chat_id = _callback_chat_id(cb)
        if app.stage_of(user.id) == "pd":
            await _ack(cb)
            await app.show_pd(chat_id)
            return
        row = app.db.get_user(user.id)
        already = row is not None and row["mailing_consent"] is not None
        pending = row["pending_source"] if row else None
        if already and not pending and app._already_in_funnel(user.id):
            await _ack(cb)
            await _drop_markup(cb)
            return
        if not already:
            app.accept_mailing(user.id, cb.data == CB_MAIL_YES)
        await _ack(cb)
        await _drop_markup(cb)
        await app.finish_and_deliver(chat_id, user)

    # ----- воронка -----
    @r.callback_query(F.data.startswith("lead:"))
    async def on_lead_button(cb: CallbackQuery):
        source = cb.data.split(":", 1)[1]
        await cb.answer()
        if source in t["sources"]:
            await app.deliver_lead(cb.message.chat.id, cb.from_user, source)

    @r.message(F.video | F.animation | F.document)
    async def on_media(message: Message):
        """Елена присылает видео боту — бот отвечает его file_id для .env."""
        if not app.is_admin(message.from_user.id):
            await message.answer(fmt(t["unknown"]), reply_markup=app.kb_choice())
            return
        media = message.video or message.animation or message.document
        await message.answer(fmt(a["file_id"], file_id=media.file_id))

    @r.message(F.text)
    async def on_text(message: Message):
        source = app.funnel.source_from_text(message.text)
        user = message.from_user
        if source:
            app.funnel.register_start(user.id, user.username, user.full_name, source)
            await app.deliver_lead(message.chat.id, user, source)
        else:
            await message.answer(fmt(t["unknown"]), reply_markup=app.kb_choice())

    gate = ConsentMiddleware(app)
    r.message.outer_middleware(gate)
    r.callback_query.outer_middleware(gate)
    return r


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    texts = load_texts()
    db = DB(settings.db_path)
    log.info("База данных: %s", settings.db_path)
    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML,
                                                                link_preview_is_disabled=True))
    app = App(settings, texts, db, bot)
    dp = Dispatcher()
    dp.include_router(build_router(app))

    if not settings.admin_ids:
        log.warning("ADMIN_ID не задан: /stats и /broadcast не работают. Напишите боту /myid и впишите число")
    if not settings.landing_url:
        log.warning("LANDING_URL не задан: кнопка «Посмотреть программу» не показывается")
    if not settings.channel_url:
        log.warning("CHANNEL_URL не задан: приглашение в канал не отправляется")

    me = await bot.get_me()
    log.info("Бот @%s запущен. Ссылки: https://t.me/%s?start=utechki и https://t.me/%s?start=agent",
             me.username, me.username, me.username)
    await bot.set_my_commands([
        BotCommand(command="start", description="Начать"),
        BotCommand(command="stop", description="Отписаться от рассылки"),
    ])
    await bot.delete_webhook(drop_pending_updates=False)  # работаем через long polling

    scheduler = asyncio.create_task(app.scheduler_loop())
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        scheduler.cancel()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit) as e:
        if isinstance(e, SystemExit) and e.code not in (None, 0):
            raise
