"""Настройки бота. Всё берётся из переменных окружения или файла .env.

Порядок: если переменная (например BOT_TOKEN) уже задана в окружении,
используется она. Если нет — значение из файла .env.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
# override=False: переменные окружения важнее файла .env
load_dotenv(BASE_DIR / ".env", override=False)


def _str(name: str, default: str = "") -> str:
    value = os.getenv(name, default)
    return value.strip() if value else ""


def _int(name: str, default: int | None = None) -> int | None:
    raw = _str(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise SystemExit(f"Настройка {name} должна быть целым числом, сейчас: {raw!r}")


def _float(name: str, default: float) -> float:
    raw = _str(name)
    if not raw:
        return default
    try:
        return float(raw.replace(",", "."))
    except ValueError:
        raise SystemExit(f"Настройка {name} должна быть числом, сейчас: {raw!r}")


def _bool(name: str, default: bool = False) -> bool:
    raw = _str(name).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "да", "on"}


@dataclass
class Settings:
    bot_token: str
    admin_ids: list[int]
    # лид-магниты
    lead_utechki_url: str
    lead_utechki_file_id: str
    lead_agent_url: str
    lead_agent_file_id: str
    # ссылки
    channel_url: str
    # лендинг курса (оплата только там, через Prodamus; бот оплату не отслеживает)
    landing_url: str
    # отложенные сообщения (после видео)
    program_delay_minutes: float  # сообщение с программой; 0 = отправить сразу
    channel_invite_delay_hours: float
    warmup1_delay_hours: float
    warmup2_delay_hours: float
    timezone: str
    send_from_hour: int
    send_to_hour: int
    scheduler_interval_seconds: int
    # рассылка: сколько сообщений в секунду (Telegram разрешает ~30, держимся ниже)
    broadcast_rate_per_sec: float = 15.0
    db_path: Path = field(default_factory=lambda: BASE_DIR / "data" / "bot.sqlite3")


def load_settings(require_token: bool = True) -> Settings:
    # Bothost сам передаёт токен в BOT_TOKEN (а также API_TOKEN и TELEGRAM_BOT_TOKEN)
    token = _str("BOT_TOKEN") or _str("TELEGRAM_BOT_TOKEN") or _str("API_TOKEN")
    if require_token and not token:
        raise SystemExit(
            "Не задан BOT_TOKEN. Впишите токен от @BotFather в файл .env "
            "или задайте переменную окружения BOT_TOKEN."
        )
    admin_ids = []
    for part in _str("ADMIN_ID").replace(";", ",").split(","):
        part = part.strip()
        if part:
            try:
                admin_ids.append(int(part))
            except ValueError:
                raise SystemExit(f"ADMIN_ID должен быть числом (или числами через запятую), сейчас: {part!r}")

    # База: DB_PATH / DATABASE_PATH → папка DATA_DIR (на Bothost это /app/data, она не стирается
    # при обновлении) → папка data рядом с кодом.
    db_raw = _str("DB_PATH") or _str("DATABASE_PATH")
    if db_raw:
        db_path = Path(db_raw)
    else:
        data_dir = _str("DATA_DIR")
        db_path = (Path(data_dir) if data_dir else BASE_DIR / "data") / "bot.sqlite3"
    if not db_path.is_absolute():
        db_path = BASE_DIR / db_path

    return Settings(
        bot_token=token,
        admin_ids=admin_ids,
        lead_utechki_url=_str("LEAD_UTECHKI_URL"),
        lead_utechki_file_id=_str("LEAD_UTECHKI_FILE_ID"),
        lead_agent_url=_str("LEAD_AGENT_URL"),
        lead_agent_file_id=_str("LEAD_AGENT_FILE_ID"),
        channel_url=_str("CHANNEL_URL"),
        landing_url=_str("LANDING_URL"),
        program_delay_minutes=_float("PROGRAM_DELAY_MINUTES", 10),
        channel_invite_delay_hours=_float("CHANNEL_INVITE_DELAY_HOURS", 2),
        warmup1_delay_hours=_float("WARMUP1_DELAY_HOURS", 24),
        warmup2_delay_hours=_float("WARMUP2_DELAY_HOURS", 60),
        timezone=_str("TIMEZONE") or "Europe/Moscow",
        send_from_hour=_int("SEND_FROM_HOUR", 10) or 0,
        send_to_hour=_int("SEND_TO_HOUR", 21) or 0,
        scheduler_interval_seconds=_int("SCHEDULER_INTERVAL_SECONDS", 60) or 60,
        broadcast_rate_per_sec=min(max(_float("BROADCAST_RATE_PER_SEC", 15), 0.5), 20),
        db_path=db_path,
    )
