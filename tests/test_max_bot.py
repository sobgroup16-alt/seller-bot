"""Бот MAX: тот же сценарий, что у Telegram, без настоящих запросов к platform-api."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from bot import CB_MAIL_NO, CB_MAIL_YES, CB_PD
from config import Settings
from db import DB
from max_api import MaxApiError, MaxClient, is_blocked, is_retryable
from max_bot import MaxApp
from texts import load_texts

TEXTS = load_texts()


class Recorder:
    def __init__(self, routes=None):
        self.calls = []
        self.routes = routes or {}

    async def __call__(self, method, path, params, body, headers):
        self.calls.append({
            "method": method, "path": path, "params": params, "body": body, "headers": headers,
        })
        if path in self.routes:
            return self.routes[path]
        if path == "/answers":
            return 200, {"success": True}
        if path == "/messages":
            return 200, {"message": {"body": {"mid": "m1"}}}
        return 200, {}


def settings(**kw) -> Settings:
    base = dict(
        bot_token="tg-not-used",
        admin_ids=[42],
        lead_utechki_url="https://example.com/utechki",
        lead_utechki_file_id="",
        lead_agent_url="https://example.com/agent",
        lead_agent_file_id="",
        channel_url="https://max.ru/channel",
        landing_url="https://ecomlena.ru/ai/",
        program_delay_minutes=10,
        channel_invite_delay_hours=2,
        warmup1_delay_hours=24,
        warmup2_delay_hours=60,
        timezone="Europe/Moscow",
        send_from_hour=10,
        send_to_hour=21,
        scheduler_interval_seconds=60,
        max_admin_ids=[42],
    )
    base.update(kw)
    return Settings(**base)


def make_app(**kw):
    rec = Recorder()
    api = MaxClient("max-secret-token", transport=rec)
    app = MaxApp(settings(**kw), TEXTS, DB(":memory:"), api)
    return app, rec


def texts_of(rec: Recorder) -> list[str]:
    return [call["body"]["text"] for call in rec.calls if call["path"] == "/messages"]


def buttons_of(rec: Recorder) -> list:
    found = []
    for call in rec.calls:
        if call["path"] != "/messages":
            continue
        for attachment in (call["body"] or {}).get("attachments") or []:
            if attachment.get("type") == "inline_keyboard":
                found.append(attachment["payload"]["buttons"])
    return found


def started(uid=7, payload="utechki", first_name="Анна"):
    return {
        "update_type": "bot_started",
        "chat_id": uid,
        "user": {"user_id": uid, "first_name": first_name, "username": "anna", "is_bot": False},
        "payload": payload,
    }


def message(text, uid=7):
    return {
        "update_type": "message_created",
        "message": {
            "sender": {"user_id": uid, "first_name": "Анна", "username": "anna", "is_bot": False},
            "body": {"text": text},
        },
    }


def callback(payload, uid=7, callback_id="cb-1"):
    return {
        "update_type": "message_callback",
        "callback": {
            "callback_id": callback_id,
            "payload": payload,
            "user": {"user_id": uid, "first_name": "Анна", "username": "anna", "is_bot": False},
        },
    }


def steps(app: MaxApp, user_id: int) -> set[str]:
    rows = app.db.conn.execute(
        "SELECT step FROM scheduled WHERE user_id=? AND cancelled=0", (user_id,)
    )
    return {row[0] for row in rows}


def test_client_sends_token_only_in_header_and_html_message():
    async def run():
        rec = Recorder()
        api = MaxClient("max-secret-token", transport=rec)
        await api.send_message(7, "<b>Привет</b>", [[{"type": "link", "text": "Сайт", "url": "https://ecomlena.ru/ai/"}]])
        call = rec.calls[0]
        assert call["headers"]["Authorization"] == "max-secret-token"
        assert "Bearer" not in call["headers"]["Authorization"]
        assert "access_token" not in (call["params"] or {})
        assert call["params"]["user_id"] == 7
        assert call["params"]["disable_link_preview"] == "true"
        assert call["body"]["format"] == "html"
        assert call["body"]["text"] == "<b>Привет</b>"
        await api.get_updates(marker=None, types=["bot_started", "message_callback"])
        polling = rec.calls[1]
        assert "marker" not in polling["params"]
        assert polling["params"]["types"] == "bot_started,message_callback"
        assert "message_callback" in polling["params"]["types"]
        await api.get_updates(marker=15)
        assert rec.calls[2]["params"]["marker"] == 15

    asyncio.run(run())


def test_blocked_and_rate_limit_codes():
    assert is_blocked(MaxApiError(403, "chat.denied", "нет диалога"))
    assert is_blocked(MaxApiError(400, "chat.denied", "закрыт"))
    assert is_retryable(MaxApiError(429, "too.many.requests", "wait", retry_after=2))
    assert not is_blocked(MaxApiError(400, "proto.payload", "плохое тело"))


def test_deep_link_waits_for_both_consents_then_sends_lead():
    async def run():
        app, rec = make_app()
        await app.handle_update(started())
        assert app.db.get_pending_source(7) == "utechki"
        assert "privacy-policy" in texts_of(rec)[-1]
        assert "soglasie-pdn" in texts_of(rec)[-1]
        assert buttons_of(rec)[-1][0][0]["payload"] == CB_PD
        assert buttons_of(rec)[-1][0][0]["type"] == "callback"
        assert not any("5 утечек магазина" in text for text in texts_of(rec))

        await app.handle_update(message("/stats"))
        assert len(texts_of(rec)) == 2
        assert all("политикой обработки" in text for text in texts_of(rec))

        await app.handle_update(message("УТЕЧКИ"))
        assert app.db.get_pending_source(7) == "utechki"

        await app.handle_update(callback(CB_PD, callback_id="pd"))
        assert app.db.get_user(7)["pd_consent"] == 1
        assert app.db.get_user(7)["pd_consent_at"] is not None
        assert "soglasie-rassylka" in texts_of(rec)[-1]
        assert {row[0]["payload"] for row in buttons_of(rec)[-1]} == {CB_MAIL_YES, CB_MAIL_NO}
        assert any(call["path"] == "/answers" and call["params"]["callback_id"] == "pd" for call in rec.calls)

        await app.handle_update(callback(CB_MAIL_NO, callback_id="no"))
        assert app.db.get_user(7)["mailing_consent"] == 0
        assert any("5 утечек магазина" in text for text in texts_of(rec))
        lead = next(call for call in rec.calls if call["path"] == "/messages" and "5 утечек" in call["body"]["text"])
        link = lead["body"]["attachments"][0]["payload"]["buttons"][0][0]
        assert link["type"] == "link"
        assert link["url"] == "https://example.com/utechki"
        assert steps(app, 7) == {"after_lead"}
        assert app.db.broadcast_recipients("all") == []

        before = len(texts_of(rec))
        await app.handle_update(callback(CB_MAIL_NO, callback_id="no-2"))
        assert len(texts_of(rec)) == before

    asyncio.run(run())


def test_mailing_yes_schedules_warmup_and_program_link():
    async def run():
        app, rec = make_app()
        await app.handle_update(started(payload="agent"))
        await app.handle_update(callback(CB_PD))
        await app.handle_update(callback(CB_MAIL_YES))
        assert steps(app, 7) == {"after_lead", "channel_invite", "warmup1", "warmup2"}
        assert app.db.broadcast_recipients("all") == [7]
        await app.send_scheduled(7, "after_lead", "agent")
        program = texts_of(rec)[-1]
        assert "Прибыльный ИИ-отдел" in program
        link = buttons_of(rec)[-1][0][0]
        assert link["type"] == "link"
        assert link["url"] == "https://ecomlena.ru/ai/?src=agent#program"
        await app.send_scheduled(7, "warmup1", "agent")
        warmup = texts_of(rec)[-1]
        assert "2007" in warmup
        assert "шоколада и мармелада" in warmup
        assert "30 000" not in warmup
        assert "1 млн" not in warmup

    asyncio.run(run())


def test_stop_and_bot_stopped():
    async def run():
        app, rec = make_app()
        await app.handle_update(started())
        await app.handle_update(message("/stop"))
        assert texts_of(rec)[-1] == "Вы отписались от рассылки. Материалы и бот остаются доступны."
        assert app.db.get_user(7)["mailing_consent"] == 0
        assert app.db.get_user(7)["blocked"] == 0
        assert "5 утечек" not in texts_of(rec)[-1]

        await app.handle_update(callback(CB_PD))
        assert any("5 утечек магазина" in text for text in texts_of(rec))

        await app.handle_update({
            "update_type": "bot_stopped",
            "user": {"user_id": 7, "first_name": "Анна", "is_bot": False},
        })
        assert app.db.get_user(7)["blocked"] == 1

    asyncio.run(run())


def test_telegram_file_id_is_not_sent_as_video():
    async def run():
        app, rec = make_app(lead_utechki_url="", lead_utechki_file_id="tg-file")
        app.note_user(type("U", (), {"id": 7, "username": "a", "full_name": "Анна", "first_name": "Анна"})())
        app.accept_pd(7)
        app.accept_mailing(7, True)
        app.db.set_pending_source(7, "utechki")
        user = app.db.get_user(7)
        person = type("U", (), {
            "id": 7, "username": "a", "full_name": user["full_name"], "first_name": "Анна",
        })()
        await app.finish_and_deliver(7, person)
        assert any("Видео скоро будет здесь" in text for text in texts_of(rec))
        assert all(call["path"] in {"/messages", "/answers"} for call in rec.calls)

    asyncio.run(run())


def test_scheduler_skips_promo_without_mailing_consent():
    async def run():
        app, _rec = make_app()
        sent = []

        async def send(user_id, step, source):
            sent.append(step)
            return "ok"

        past = datetime.now(timezone.utc) - timedelta(minutes=5)
        app.db.ensure_user(4, "a", "Анна")
        app.db.schedule(4, [(step, past) for step in ("after_lead", "channel_invite", "warmup1", "warmup2")])
        delivered = await app.funnel.process_due(send)
        assert delivered == 1
        assert sent == ["after_lead"]

    asyncio.run(run())


def test_start_without_payload_shows_choice_after_consent():
    async def run():
        app, rec = make_app()
        await app.handle_update(started(payload=None))
        await app.handle_update(callback(CB_PD))
        await app.handle_update(callback(CB_MAIL_YES))
        assert "Что вам прислать?" in texts_of(rec)[-1]
        assert buttons_of(rec)[-1][0][0]["payload"] == "lead:utechki"

    asyncio.run(run())


class WebhookAPI:
    """Память подписок: GET отдаёт список, DELETE снимает url."""

    def __init__(self, subs, fail_delete=None, fail_get=False):
        self.subs = list(subs)
        self.fail_delete = set(fail_delete or [])
        self.fail_get = fail_get
        self.calls = []
        self.gets = 0

    async def __call__(self, method, path, params, body, headers):
        self.calls.append({"method": method, "path": path, "params": params})
        if path != "/subscriptions":
            return 200, {}
        if method == "GET":
            self.gets += 1
            if self.fail_get and self.gets == 1:
                return 500, {"code": "internal", "message": "временно недоступно"}
            return 200, {"subscriptions": [{"url": url} for url in self.subs]}
        if method == "DELETE":
            url = (params or {}).get("url")
            if url in self.fail_delete:
                return 200, {"success": False, "code": "not.found", "message": "нет такой подписки"}
            self.subs = [item for item in self.subs if item != url]
            return 200, {"success": True}
        return 200, {}


def _app_with(api: WebhookAPI) -> MaxApp:
    client = MaxClient("max-secret-token", transport=api)
    return MaxApp(settings(), TEXTS, DB(":memory:"), client)


def test_startup_deletes_every_webhook_then_checks_the_list_is_empty(caplog):
    xl = "https://cht-xl.xlcrm.ai/api/v1/channel-webhook/max/MzM2NTg1ODIw"
    other = "https://example.com/hook"
    api = WebhookAPI([xl, other])
    app = _app_with(api)

    async def run():
        with caplog.at_level(logging.INFO, logger="max_bot"):
            await app.prepare_updates()

    asyncio.run(run())
    deleted = [call["params"]["url"] for call in api.calls if call["method"] == "DELETE"]
    assert deleted == [xl, other]
    assert api.subs == []
    assert api.gets == 2
    assert f"Удалена подписка Webhook: {xl}" in caplog.text
    assert f"Удалена подписка Webhook: {other}" in caplog.text
    assert "Подписок Webhook нет" in caplog.text


def test_delete_error_does_not_stop_the_rest(caplog):
    keep = "https://example.com/stuck"
    gone = "https://example.com/ok"
    api = WebhookAPI([keep, gone], fail_delete={keep})
    app = _app_with(api)

    async def run():
        with caplog.at_level(logging.INFO, logger="max_bot"):
            await app.prepare_updates()

    asyncio.run(run())
    assert api.subs == [keep]
    assert f"Удалена подписка Webhook: {gone}" in caplog.text
    assert "Не удалось удалить Webhook" in caplog.text
    assert keep in caplog.text
    assert "Подписки Webhook остались" in caplog.text


def test_list_error_is_logged_and_polling_setup_continues(caplog):
    api = WebhookAPI(["https://example.com/hook"], fail_get=True)
    app = _app_with(api)

    async def run():
        with caplog.at_level(logging.WARNING, logger="max_bot"):
            await app.prepare_updates()

    asyncio.run(run())
    assert not any(call["method"] == "DELETE" for call in api.calls)
    assert "Не удалось получить подписки Webhook" in caplog.text


def test_keep_webhooks_env_does_not_delete(monkeypatch, caplog):
    monkeypatch.setenv("MAX_KEEP_WEBHOOKS", "1")
    api = WebhookAPI(["https://example.com/hook"])
    app = _app_with(api)

    async def run():
        with caplog.at_level(logging.INFO, logger="max_bot"):
            await app.prepare_updates()

    asyncio.run(run())
    assert not any(call["method"] == "DELETE" for call in api.calls)
    assert api.subs == ["https://example.com/hook"]
    assert "MAX_KEEP_WEBHOOKS=1" in caplog.text
    assert "У бота есть Webhook" in caplog.text


def test_button_press_without_callback_user_still_opens_the_next_step():
    """Реальный message_callback: sender — бот, человек в recipient."""

    async def run():
        app, rec = make_app()
        await app.handle_update(started())
        await app.handle_update({
            "update_type": "message_callback",
            "callback": {"callback_id": "cb-real", "payload": CB_PD},
            "message": {
                "sender": {"user_id": 999, "first_name": "Bot", "is_bot": True},
                "recipient": {"chat_id": 7, "chat_type": "dialog", "user_id": 7},
            },
        })
        assert app.db.get_user(7)["pd_consent"] == 1
        assert "soglasie-rassylka" in texts_of(rec)[-1]
        assert any(call["path"] == "/answers" and call["params"]["callback_id"] == "cb-real" for call in rec.calls)

    asyncio.run(run())


def test_document_links_stay_on_consent_screens():
    pd = TEXTS["consent"]["pd"]
    assert "https://ecomlena.ru/privacy-policy/" in pd
    assert "https://ecomlena.ru/soglasie-pdn/" in pd
    assert "https://ecomlena.ru/soglasie-rassylka/" in TEXTS["consent"]["mailing"]
