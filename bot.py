"""Telegram-бот Елены Тимошенко: лид-магниты по кодовым словам, ссылка на программу курса,
приглашение в канал, прогрев и рассылка по базе.

Оплаты в боте нет: курс оплачивается на лендинге (Prodamus), бот оплату не отслеживает.

Запуск: python main.py   (или ./run.sh — с автоперезапуском и логом)
"""
from __future__ import annotations

import asyncio
import html
import logging
from datetime import datetime

from aiogram import Bot, Dispatcher, F, Router
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
    User,
)
from zoneinfo import ZoneInfo

from broadcast import run_broadcast
from config import Settings, load_settings
from db import AUDIENCES, DB
from funnel import Funnel
from logic import landing_link, parse_button_line, strip_last_line
from texts import fmt, load_texts

log = logging.getLogger("bot")

# Ошибки Telegram, после которых человеку больше не пишем
GONE_MARKERS = ("chat not found", "user is deactivated", "bot was blocked", "user not found",
                "peer_id_invalid", "bot can't initiate conversation")


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
        # сразу после видео: короткий прогрев + кнопка «Посмотреть программу»
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


def build_router(app: App) -> Router:
    r = Router()
    t = app.t
    a = t["admin"]

    @r.message(CommandStart())
    async def on_start(message: Message, command: CommandObject):
        user = message.from_user
        source = app.funnel.source_from_start(command.args)
        app.funnel.register_start(user.id, user.username, user.full_name, source)
        if source:
            await app.deliver_lead(message.chat.id, user, source)
        else:
            await message.answer(fmt(t["welcome"]), reply_markup=app.kb_choice())

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
    await bot.set_my_commands([BotCommand(command="start", description="Начать")])
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
