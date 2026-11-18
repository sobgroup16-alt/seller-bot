"""Рассылка по базе: отправка с ограничением скорости и обработкой ошибок. Без Telegram, чтобы легко тестировать.

send_one(user_id) возвращает:
  'ok'               — доставлено;
  'blocked'          — человек остановил бота или удалил аккаунт (помечаем и больше не пишем);
  'failed'           — другая ошибка, пропускаем;
  ('retry', секунды) — Telegram просит подождать (flood control), ждём и пробуем ещё раз.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Iterable

log = logging.getLogger("broadcast")

# Telegram разрешает ~30 сообщений в секунду всем людям вместе. Держимся с запасом.
MAX_RATE_PER_SEC = 20.0


@dataclass
class BroadcastResult:
    total: int = 0
    sent: int = 0
    failed: int = 0
    blocked: int = 0


async def run_broadcast(
    user_ids: Iterable[int],
    send_one: Callable[[int], Awaitable[object]],
    rate_per_sec: float = 15.0,
    on_blocked: Callable[[int], None] | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    max_retries: int = 3,
) -> BroadcastResult:
    rate = min(max(rate_per_sec, 0.1), MAX_RATE_PER_SEC)
    pause = 1.0 / rate
    ids = list(dict.fromkeys(user_ids))  # без дублей, порядок сохраняется
    res = BroadcastResult(total=len(ids))
    for n, uid in enumerate(ids):
        if n:
            await sleep(pause)
        attempt = 0
        while True:
            try:
                outcome = await send_one(uid)
            except Exception:  # noqa: BLE001 — один сбой не останавливает рассылку
                log.exception("Рассылка: ошибка для %s", uid)
                outcome = "failed"
            if isinstance(outcome, tuple) and outcome and outcome[0] == "retry":
                attempt += 1
                if attempt > max_retries:
                    outcome = "failed"
                else:
                    await sleep(float(outcome[1]) + 1)
                    continue
            break
        if outcome == "ok":
            res.sent += 1
        elif outcome == "blocked":
            res.blocked += 1
            if on_blocked:
                on_blocked(uid)
        else:
            res.failed += 1
    return res
