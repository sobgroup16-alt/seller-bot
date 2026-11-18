"""Воронка: что происходит с человеком на каждом шаге. Без Telegram, чтобы легко тестировать."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Awaitable, Callable

from config import Settings
from db import DB, from_ts, now_ts
from logic import build_word_index, parse_code_word, parse_start_payload, plan_warmups

log = logging.getLogger("funnel")


class Funnel:
    def __init__(self, db: DB, settings: Settings, texts: dict):
        self.db = db
        self.s = settings
        self.texts = texts
        self.word_index = build_word_index(texts["sources"])

    # ---------- кодовые слова ----------
    def source_from_start(self, payload: str | None) -> str | None:
        return parse_start_payload(payload, self.word_index)

    def source_from_text(self, text: str | None) -> str | None:
        return parse_code_word(text or "", self.word_index)

    def source_title(self, source: str | None) -> str:
        if source and source in self.texts["sources"]:
            return self.texts["sources"][source].get("title", source)
        return "без кодового слова"

    # ---------- шаги ----------
    def register_start(self, user_id: int, username: str | None, full_name: str | None,
                       source: str | None = None, ts: int | None = None) -> None:
        self.db.upsert_user(user_id, username, full_name, source, ts=ts)
        self.db.log_event(user_id, "start", source, ts=ts)

    def register_lead(self, user_id: int, username: str | None, full_name: str | None,
                      source: str, ts: int | None = None) -> bool:
        """Человек получил лид-магнит. Полный прогрев ставится один раз.

        Сообщение с программой (after_lead) планируется при каждой выдаче видео:
        раньше оно уходило сразу после каждого ролика. Остальные шаги при повторной
        выдаче не дублируются. Возвращает True, если поставлен полный план (первый раз).
        Оплату бот не отслеживает: приглашение в канал и прогрев получают все,
        кто не остановил бота.
        """
        ts = ts or now_ts()
        self.db.upsert_user(user_id, username, full_name, source, ts=ts)
        self.db.log_event(user_id, f"lead_{source}", source, ts=ts)
        delays = {
            "after_lead": (self.s.program_delay_minutes or 0) / 60,
            "channel_invite": self.s.channel_invite_delay_hours if self.s.channel_url else 0,
            "warmup1": self.s.warmup1_delay_hours,
            "warmup2": self.s.warmup2_delay_hours,
        }
        plan = plan_warmups(from_ts(ts), delays, self.s.timezone, self.s.send_from_hour, self.s.send_to_hour)
        if not self.db.has_schedule(user_id):
            self.db.schedule(user_id, plan)
            return bool(plan)
        # Повторная выдача: полный прогрев не дублируем, программу ставим заново.
        for step, due in plan:
            if step == "after_lead":
                self.db.reschedule(user_id, step, due)
        return False

    # ---------- отложенные сообщения ----------
    async def process_due(self, send: Callable[[int, str, str | None], Awaitable[str]],
                          ts: int | None = None) -> int:
        """Отправляет все сообщения, которым пора уйти.

        send(user_id, step, source) должен вернуть 'ok', 'blocked' или 'retry'.
        """
        sent = 0
        for row in self.db.due_messages(ts):
            # повторная проверка прямо перед отправкой
            user = self.db.get_user(row["user_id"])
            if user["blocked"]:
                self.db.cancel_one(row["id"])
                continue
            if row["step"] == "channel_invite" and not self.s.channel_url:
                self.db.cancel_one(row["id"])
                continue
            try:
                result = await send(row["user_id"], row["step"], row["last_source"] or row["source"])
            except Exception:  # noqa: BLE001 — одна ошибка не должна останавливать очередь
                log.exception("Не удалось отправить %s пользователю %s", row["step"], row["user_id"])
                result = "retry"
            if result == "ok":
                self.db.mark_sent(row["id"], ts)
                self.db.log_event(row["user_id"], row["step"], row["last_source"] or row["source"], ts=ts)
                sent += 1
            elif result == "blocked":
                self.db.mark_blocked(row["user_id"])
        return sent


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
