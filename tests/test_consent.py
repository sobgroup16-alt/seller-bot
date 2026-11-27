"""Согласия: хранение, выдача материала, прогрев и рассылка."""
from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone

from aiogram.types import CallbackQuery, Chat, Message, User

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.types import Update

from bot import CB_MAIL_NO, CB_MAIL_YES, CB_PD, App, ConsentMiddleware, build_router
from config import Settings
from db import PROMO_STEPS, DB
from texts import fmt, load_texts

TEXTS = load_texts()


class FakeBot:
    def __init__(self):
        self.out: list[dict] = []

    async def send_message(self, chat_id, text, reply_markup=None, **kwargs):
        self.out.append({
            "method": "message", "chat_id": chat_id, "text": text, "reply_markup": reply_markup,
        })

    async def send_video(self, chat_id, video, caption=None, reply_markup=None, **kwargs):
        self.out.append({"method": "video", "chat_id": chat_id, "caption": caption})

    async def send_photo(self, chat_id, photo, caption=None, reply_markup=None, **kwargs):
        self.out.append({"method": "photo", "chat_id": chat_id, "caption": caption})


def settings(**kw) -> Settings:
    base = dict(
        bot_token="123:TEST",
        admin_ids=[],
        lead_utechki_url="https://example.com/utechki",
        lead_utechki_file_id="",
        lead_agent_url="https://example.com/agent",
        lead_agent_file_id="",
        channel_url="https://t.me/channel",
        landing_url="https://ecomlena.ru/course",
        program_delay_minutes=10,
        channel_invite_delay_hours=2,
        warmup1_delay_hours=24,
        warmup2_delay_hours=60,
        timezone="Europe/Moscow",
        send_from_hour=10,
        send_to_hour=21,
        scheduler_interval_seconds=60,
    )
    base.update(kw)
    return Settings(**base)


def make_app(db: DB | None = None) -> tuple[App, FakeBot]:
    bot = FakeBot()
    app = App(settings(), TEXTS, db or DB(":memory:"), bot)
    return app, bot


def person(uid: int = 7) -> User:
    return User(id=uid, is_bot=False, first_name="Анна", username="anna")


def message(text: str | None, uid: int = 7, caption: str | None = None) -> Message:
    user = person(uid)
    return Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=Chat(id=uid, type="private"),
        from_user=user,
        text=text,
        caption=caption,
    )


def callback(data: str, uid: int = 7) -> CallbackQuery:
    return CallbackQuery(
        id="cb1",
        from_user=person(uid),
        chat_instance="chat",
        data=data,
    )


def steps(db: DB, user_id: int, cancelled: int | None = 0) -> set[str]:
    if cancelled is None:
        rows = db.conn.execute("SELECT step FROM scheduled WHERE user_id=?", (user_id,))
    else:
        rows = db.conn.execute(
            "SELECT step FROM scheduled WHERE user_id=? AND cancelled=?", (user_id, cancelled)
        )
    return {row[0] for row in rows}


def test_texts_are_exact():
    pd = fmt(TEXTS["consent"]["pd"])
    assert pd.startswith("Привет! Я бот Елены Тимошенко, ECOMLENA.")
    assert "https://ecomlena.ru/privacy-policy/" in pd
    assert "https://ecomlena.ru/soglasie-pdn/" in pd
    assert "политикой обработки персональных данных" in pd
    assert "согласие на обработку данных" in pd
    mailing = fmt(TEXTS["consent"]["mailing"])
    assert mailing.startswith(
        "Спасибо! И последний вопрос: можно иногда присылать вам полезные разборы и предложения?"
    )
    assert "https://ecomlena.ru/soglasie-rassylka/" in mailing
    assert "Условия рассылки" in mailing
    assert fmt(TEXTS["consent"]["unsubscribed"]) == (
        "Вы отписались от рассылки. Материалы и бот остаются доступны."
    )
    assert TEXTS["buttons"]["pd_consent"] == "Согласна(сен) на обработку персональных данных"
    assert TEXTS["buttons"]["mailing_yes"] == "Да, присылайте"
    assert TEXTS["buttons"]["mailing_no"] == "Только материал"
    assert len(TEXTS["buttons"]["pd_consent"]) <= 64


def test_old_database_gains_columns_without_losing_people(tmp_path):
    path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            full_name TEXT,
            source TEXT,
            last_source TEXT,
            created_at INTEGER NOT NULL,
            blocked INTEGER NOT NULL DEFAULT 0
        )"""
    )
    conn.execute(
        "INSERT INTO users(user_id, username, full_name, source, created_at, blocked) VALUES(1,'old','Ольга','utechki',100,0)"
    )
    conn.commit()
    conn.close()

    db = DB(path)
    row = db.get_user(1)
    assert row["source"] == "utechki"
    assert row["pd_consent"] is None
    assert row["mailing_consent"] is None
    assert row["pd_consent_at"] is None
    assert db.broadcast_recipients("all") == []
    assert db.broadcast_recipients("utechki") == []
    # повторное открытие не падает на уже добавленных колонках
    DB(path)


def test_consent_timestamps_are_kept_as_proof():
    db = DB(":memory:")
    db.ensure_user(5, "ann", "Анна", ts=1)
    db.set_pd_consent(5, ts=1_700_000_000)
    db.set_pd_consent(5, ts=1_800_000_000)
    row = db.get_user(5)
    assert row["pd_consent"] == 1
    assert row["pd_consent_at"] == 1_700_000_000
    db.set_mailing_consent(5, True, ts=1_700_000_100)
    db.set_mailing_consent(5, False, ts=1_700_000_500)
    row = db.get_user(5)
    assert row["mailing_consent"] == 0
    assert row["mailing_consent_at"] == 1_700_000_500
    assert row["blocked"] == 0


def test_opt_in_clears_blocked_flag():
    db = DB(":memory:")
    db.upsert_user(5, "ann", "Анна", "utechki")
    db.mark_blocked(5)
    db.set_mailing_consent(5, True, ts=10)
    assert db.get_user(5)["blocked"] == 0
    assert db.mailing_allowed(5)


def test_existing_user_is_excluded_from_promo_until_opt_in():
    db = DB(":memory:")
    db.upsert_user(5, "ann", "Анна", "utechki", ts=1)
    past = int((datetime.now(timezone.utc) - timedelta(hours=1)).timestamp())
    for step in ("after_lead", *PROMO_STEPS):
        db.conn.execute(
            "INSERT INTO scheduled(user_id, step, due_at) VALUES(?,?,?)", (5, step, past)
        )
    db.conn.commit()
    due = {row["step"] for row in db.due_messages(limit=10)}
    assert due == {"after_lead"}
    assert db.broadcast_recipients("all") == []

    db.set_mailing_consent(5, True, ts=past)
    due = {row["step"] for row in db.due_messages(limit=10)}
    assert due == {"after_lead", *PROMO_STEPS}
    assert db.broadcast_recipients("all") == [5]
    assert db.broadcast_recipients("utechki") == [5]
    assert db.broadcast_recipients("agent") == []


def test_promo_without_consent_does_not_clog_the_queue():
    db = DB(":memory:")
    past = 1_000
    for uid in range(1, 61):
        db.upsert_user(uid, None, None, "utechki", ts=uid)
        db.conn.execute(
            "INSERT INTO scheduled(user_id, step, due_at) VALUES(?,?,?)", (uid, "warmup1", past)
        )
    db.upsert_user(1000, None, None, "agent", ts=1000)
    db.conn.execute(
        "INSERT INTO scheduled(user_id, step, due_at) VALUES(?,?,?)", (1000, "after_lead", past + 1)
    )
    db.conn.commit()
    due = db.due_messages(ts=past + 10, limit=50)
    assert [row["step"] for row in due] == ["after_lead"]


def test_stop_cancels_promo_and_keeps_program_message():
    app, _bot = make_app()
    user = person(5)
    app.note_user(user)
    app.db.set_pd_consent(5, ts=10)
    app.db.set_mailing_consent(5, True, ts=11)
    now = datetime.now(timezone.utc)
    app.db.schedule(5, [(step, now + timedelta(hours=1)) for step in ("after_lead", *PROMO_STEPS)])
    app.unsubscribe(5)
    assert app.db.get_user(5)["mailing_consent"] == 0
    assert app.db.get_user(5)["blocked"] == 0
    assert steps(app.db, 5) == {"after_lead"}
    assert app.db.has_event(5, "mailing_stop")
    assert app.db.broadcast_recipients("all") == []


def test_lead_without_mailing_schedules_only_program():
    app, _bot = make_app()
    user = person(5)
    app.note_user(user)
    app.accept_pd(5)
    app.accept_mailing(5, False)
    app.funnel.register_lead(5, "anna", "Анна Иванова", "utechki")
    assert steps(app.db, 5) == {"after_lead"}
    assert app.db.has_event(5, "pd_consent")
    assert app.db.has_event(5, "mailing_no")


def test_lead_with_mailing_schedules_warmup():
    app, _bot = make_app()
    user = person(5)
    app.note_user(user)
    app.accept_pd(5)
    app.accept_mailing(5, True)
    assert app.funnel.register_lead(5, "anna", "Анна Иванова", "agent") is True
    assert steps(app.db, 5) == {"after_lead", *PROMO_STEPS}


def test_deep_link_is_saved_and_material_follows_either_answer():
    async def scenario(agree: bool):
        app, bot = make_app()
        gate = ConsentMiddleware(app)
        called: list[str] = []

        async def handler(event, data):
            called.append("handler")

        await gate(handler, message("/start utechki"), {})
        assert called == []
        assert app.db.get_pending_source(7) == "utechki"
        assert "privacy-policy" in bot.out[-1]["text"]
        assert bot.out[-1]["reply_markup"].inline_keyboard[0][0].callback_data == CB_PD

        # другое сообщение не выдаёт материал и не стирает параметр
        await gate(handler, message("хочу сразу видео"), {})
        assert called == []
        assert app.db.get_pending_source(7) == "utechki"
        assert len(bot.out) == 2

        await gate(handler, callback("lead:agent"), {})
        assert app.db.get_pending_source(7) == "agent"
        assert called == []

        # возвращаем исходную ссылку, как будто человек открыл именно её
        app.db.set_pending_source(7, "utechki")
        user = person(7)
        app.accept_pd(user.id)
        app.accept_mailing(user.id, agree)
        await app.finish_and_deliver(7, user)
        assert any("5 утечек магазина" in item["text"] for item in bot.out)
        assert app.db.get_pending_source(7) is None
        assert app.db.has_event(7, "lead_utechki")
        expected = {"after_lead", *PROMO_STEPS} if agree else {"after_lead"}
        assert steps(app.db, 7) == expected
        # повторное нажатие не шлёт видео ещё раз
        before = len(bot.out)
        row = app.db.get_user(7)
        assert row["mailing_consent"] == (1 if agree else 0)
        assert row["pd_consent_at"] is not None
        assert row["mailing_consent_at"] is not None
        await app.finish_and_deliver(7, user)
        assert len(bot.out) == before

    asyncio.run(scenario(True))
    asyncio.run(scenario(False))


def test_start_without_payload_shows_menu_only_after_consent():
    async def run():
        app, bot = make_app()
        gate = ConsentMiddleware(app)

        async def handler(event, data):
            raise AssertionError("обработчик /start не должен работать до согласий")

        await gate(handler, message("/start"), {})
        assert app.db.get_pending_source(7) is None
        user = person(7)
        app.accept_pd(7)
        app.accept_mailing(7, False)
        await app.finish_and_deliver(7, user)
        assert "Что вам прислать?" in bot.out[-1]["text"]
        assert "Здравствуйте! Я бот Елены Тимошенко" not in bot.out[-1]["text"]
        assert bot.out[-1]["text"].startswith("Помогаю селлерам WB и Ozon")
        assert app.db.has_event(7, "start")

    asyncio.run(run())


def test_commands_before_pd_repeat_step_one_but_stop_passes():
    async def run():
        app, bot = make_app()
        gate = ConsentMiddleware(app)
        seen: list[str] = []

        async def handler(event, data):
            seen.append(event.text)

        await gate(handler, message("/stats"), {})
        await gate(handler, message("/broadcast"), {})
        await gate(handler, message(None), {})
        assert seen == []
        assert len(bot.out) == 3
        assert all("политикой обработки" in item["text"] for item in bot.out)

        await gate(handler, message("/stop"), {})
        assert seen == ["/stop"]
        # /stop до согласия на ПДн не выдаёт материал
        assert len(bot.out) == 3

    asyncio.run(run())


def test_existing_user_next_message_starts_at_step_one_and_can_opt_in():
    async def run():
        app, bot = make_app()
        # Как в базе до этой версии: человек уже получил видео, прогрев уже стоит в очереди.
        app.db.upsert_user(9, "old", "Ольга", "utechki")
        app.db.log_event(9, "start", "utechki")
        app.db.log_event(9, "lead_utechki", "utechki")
        later = datetime.now(timezone.utc) + timedelta(hours=3)
        app.db.schedule(9, [(step, later) for step in ("after_lead", *PROMO_STEPS)])
        assert "warmup1" in steps(app.db, 9)
        gate = ConsentMiddleware(app)
        called = []

        async def handler(event, data):
            called.append(True)

        await gate(handler, message("здравствуйте", uid=9), {})
        assert called == []
        assert "политикой обработки" in bot.out[-1]["text"]
        due_now = [row["step"] for row in app.db.due_messages(ts=int(later.timestamp()) + 10)]
        assert "warmup1" not in due_now
        assert "after_lead" in due_now

        user = person(9)
        app.accept_pd(9)
        assert app.stage_of(9) == "mailing"
        app.accept_mailing(9, True)
        await app.finish_and_deliver(9, user)
        # уже был в боте и новый материал не просил — видео повторно не шлём
        assert not any("5 утечек магазина" in item["text"] for item in bot.out)
        assert app.db.mailing_allowed(9)
        assert "warmup1" in steps(app.db, 9)
        due_after = [row["step"] for row in app.db.due_messages(ts=int(later.timestamp()) + 10)]
        assert "warmup1" in due_after

    asyncio.run(run())


def test_after_consent_start_reaches_the_handler():
    async def run():
        app, _bot = make_app()
        user_id = 7
        app.note_user(person(user_id))
        app.accept_pd(user_id)
        app.accept_mailing(user_id, True)
        gate = ConsentMiddleware(app)
        seen = []

        async def handler(event, data):
            seen.append(event.text)

        await gate(handler, message("/start agent"), {})
        assert seen == ["/start agent"]

    asyncio.run(run())


def test_code_word_during_consent_is_remembered_but_not_delivered():
    async def run():
        app, bot = make_app()
        gate = ConsentMiddleware(app)

        async def handler(event, data):
            raise AssertionError("кодовое слово не должно обходить согласия")

        await gate(handler, message("УТЕЧКИ"), {})
        assert app.db.get_pending_source(7) == "utechki"
        assert all("5 утечек" not in item["text"] for item in bot.out)

    asyncio.run(run())


def test_scheduler_sends_program_without_mailing_and_skips_warmup():
    async def run():
        app, _bot = make_app()
        sent: list[str] = []

        async def send(user_id, step, source):
            sent.append(step)
            return "ok"

        past = datetime.now(timezone.utc) - timedelta(minutes=5)
        app.db.ensure_user(4, "a", "Анна")
        app.db.schedule(4, [(step, past) for step in ("after_lead", *PROMO_STEPS)])
        delivered = await app.funnel.process_due(send)
        assert delivered == 1
        assert sent == ["after_lead"]
        app.db.set_mailing_consent(4, True)
        await app.funnel.process_due(send)
        assert set(sent) == {"after_lead", *PROMO_STEPS}

    asyncio.run(run())


def test_only_material_then_stop_reply_is_not_a_broadcast():
    """Сервисный ответ на /stop не идёт через список рассылки."""
    app, bot = make_app()
    user = person(3)
    app.note_user(user)
    app.accept_pd(3)
    app.db.set_pending_source(3, "agent")
    assert app.stage_of(3) == "mailing"
    waiting = app.stage_of(3) == "mailing"
    app.unsubscribe(3)
    asyncio.run(app.bot.send_message(3, fmt(TEXTS["consent"]["unsubscribed"])))
    if waiting:
        asyncio.run(app.finish_and_deliver(3, user))
    assert db_recipients_empty(app.db)
    assert any("Создай агента за вечер" in item["text"] for item in bot.out)
    assert bot.out[0]["text"] == "Вы отписались от рассылки. Материалы и бот остаются доступны."
    assert steps(app.db, 3) == {"after_lead"}


def db_recipients_empty(db: DB) -> bool:
    return db.broadcast_recipients("all") == []


class RecordingSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls: list[str] = []
        self.sent: list[str] = []

    async def close(self) -> None:
        return None

    async def make_request(self, bot, method, timeout=None):
        name = type(method).__name__
        self.calls.append(name)
        if name == "AnswerCallbackQuery":
            return True
        if name == "SendMessage":
            self.sent.append(method.text)
            return Message(
                message_id=100,
                date=datetime.now(timezone.utc),
                chat=Chat(id=method.chat_id, type="private"),
                text=method.text,
            )
        raise AssertionError(f"неожиданный запрос Telegram: {name}")

    async def stream_content(self, url, headers=None, timeout=30, chunk_size=65536, raise_for_status=True):
        if False:
            yield b""


def test_dispatcher_blocks_start_and_then_delivers_saved_material():
    """Сквозной путь: настоящие фильтры aiogram, /start=utechki не обходит согласия."""

    async def run():
        app, fake = make_app()
        session = RecordingSession()
        tg = Bot(token="123456:TEST", session=session)
        dp = Dispatcher()
        dp.include_router(build_router(app))
        try:
            await dp.feed_update(tg, Update(update_id=1, message=message("/start utechki")))
            assert app.db.get_pending_source(7) == "utechki"
            assert any("privacy-policy" in item["text"] for item in fake.out)
            assert not any("5 утечек" in item["text"] for item in fake.out)
            assert session.calls == []

            await dp.feed_update(tg, Update(update_id=2, message=message(None)))
            assert len(fake.out) == 2

            await dp.feed_update(tg, Update(update_id=3, callback_query=callback(CB_PD)))
            assert app.db.get_user(7)["pd_consent"] == 1
            assert app.db.get_user(7)["pd_consent_at"] is not None
            assert "soglasie-rassylka" in fake.out[-1]["text"]
            assert "AnswerCallbackQuery" in session.calls

            await dp.feed_update(tg, Update(update_id=4, callback_query=callback(CB_MAIL_NO)))
            assert app.db.get_user(7)["mailing_consent"] == 0
            assert any("5 утечек магазина" in item["text"] for item in fake.out)
            assert steps(app.db, 7) == {"after_lead"}
            assert app.db.broadcast_recipients("all") == []

            before = len(fake.out)
            await dp.feed_update(tg, Update(update_id=5, message=message("/stop")))
            assert app.db.has_event(7, "mailing_stop")
            assert session.calls[-1] == "SendMessage"
            assert session.sent[-1] == "Вы отписались от рассылки. Материалы и бот остаются доступны."
            # повторная выдача материала после уже завершённых согласий не происходит
            assert len(fake.out) == before
        finally:
            await tg.session.close()

    asyncio.run(run())


def test_mailing_buttons_do_not_run_before_pd():
    async def run():
        app, bot = make_app()
        user = person(8)
        app.note_user(user)
        # прямой вызов обработчика через middleware: кнопка согласия пропускается
        gate = ConsentMiddleware(app)

        async def handler(event, data):
            if event.data == CB_PD:
                app.accept_pd(event.from_user.id)
            elif event.data == CB_MAIL_YES:
                if app.stage_of(event.from_user.id) == "pd":
                    await app.show_pd(event.from_user.id)
                    return
                app.accept_mailing(event.from_user.id, True)

        await gate(handler, callback(CB_MAIL_YES, uid=8), {})
        assert app.db.get_user(8)["mailing_consent"] is None
        assert app.db.get_user(8)["pd_consent"] is None
        # handler сам показал шаг 1, потому что ПДн ещё нет
        assert "политикой обработки" in bot.out[-1]["text"]

    asyncio.run(run())


def test_funnel_import_keeps_program_step_outside_promo():
    assert "after_lead" not in PROMO_STEPS
    assert set(PROMO_STEPS) == {"channel_invite", "warmup1", "warmup2"}
