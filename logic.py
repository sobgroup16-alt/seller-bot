"""Чистая логика воронки без Telegram: разбор кодовых слов и расписание прогрева.

Эти функции проверяются тестами в tests/test_logic.py.
"""
from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# Отложенные шаги в обычном порядке: программа курса (~10 мин), приглашение в канал (~2 ч),
# история (~сутки), сомнения (~2,5 дня). Фактический порядок определяется задержками из .env.
WARMUP_STEPS = ("after_lead", "channel_invite", "warmup1", "warmup2")

# Эти шаги уходят ровно через заданную задержку, даже ночью: человек только что получил видео
# и ещё в диалоге с ботом. Остальные шаги по-прежнему сдвигаются на утро.
STEPS_WITHOUT_NIGHT_SHIFT = frozenset({"after_lead"})


def normalize(text: str) -> str:
    """Приводит текст к виду для сравнения: нижний регистр, ё→е, без знаков и пробелов по краям."""
    text = (text or "").casefold().replace("ё", "е")
    text = re.sub(r"[^\w\s-]", " ", text)  # убираем кавычки, точки, эмодзи
    return " ".join(text.split())


def build_word_index(sources: dict) -> dict[str, str]:
    """Из texts.yaml (раздел sources) строит словарь «слово → метка источника»."""
    index: dict[str, str] = {}
    for tag, info in sources.items():
        index[normalize(tag)] = tag
        for word in info.get("words", []) or []:
            index[normalize(str(word))] = tag
    return index


def parse_code_word(text: str, word_index: dict[str, str]) -> str | None:
    """Возвращает метку источника (utechki / agent), если человек написал кодовое слово.

    Понимает любой регистр («Утечки», «УТЕЧКИ»), кавычки и знаки («утечки!»),
    а также слово внутри короткой фразы («кодовое слово агент»).
    """
    norm = normalize(text)
    if not norm:
        return None
    if norm in word_index:
        return word_index[norm]
    words = norm.split()
    if len(words) <= 5:
        for w in words:
            if w in word_index:
                return word_index[w]
    return None


def parse_start_payload(payload: str | None, word_index: dict[str, str]) -> str | None:
    """Разбирает хвост ссылки t.me/бот?start=utechki → 'utechki'."""
    if not payload:
        return None
    return word_index.get(normalize(payload.replace("_", " ").replace("-", " "))) or word_index.get(
        normalize(payload)
    )


def shift_into_window(due: datetime, tz_name: str, from_hour: int, to_hour: int) -> datetime:
    """Если время отправки выпадает на ночь, переносит его на ближайшее утро.

    due — время в UTC (aware). Окно [from_hour, to_hour) в часовом поясе tz_name.
    Если from_hour == to_hour, окно выключено.
    """
    if from_hour == to_hour:
        return due
    tz = ZoneInfo(tz_name)
    local = due.astimezone(tz)
    if from_hour <= local.hour < to_hour:
        return due
    if local.hour >= to_hour:
        local = (local + timedelta(days=1)).replace(hour=from_hour, minute=0, second=0, microsecond=0)
    else:
        local = local.replace(hour=from_hour, minute=0, second=0, microsecond=0)
    return local.astimezone(due.tzinfo)


def plan_warmups(start: datetime, delays_hours: dict[str, float], tz_name: str,
                 from_hour: int, to_hour: int) -> list[tuple[str, datetime]]:
    """Возвращает список (шаг, время отправки в UTC) для отложенных сообщений.

    Шаги идут по возрастанию задержки (при равной — в порядке WARMUP_STEPS).
    Шаги с задержкой <= 0 пропускаются (так их можно выключить в .env).
    Ночное правило применяется к каждому шагу отдельно, кроме шагов из
    STEPS_WITHOUT_NIGHT_SHIFT: сообщение с программой уходит через 10 минут
    даже ночью. Каждый следующий шаг — не раньше предыдущего + 30 минут.
    """
    steps = [(delays_hours.get(st, 0) or 0, i, st) for i, st in enumerate(WARMUP_STEPS)]
    plan: list[tuple[str, datetime]] = []
    prev: datetime | None = None
    for hours, _, step in sorted(steps):
        if hours <= 0:
            continue
        due = start + timedelta(hours=hours)
        apply_night = step not in STEPS_WITHOUT_NIGHT_SHIFT
        if apply_night:
            due = shift_into_window(due, tz_name, from_hour, to_hour)
        if prev and due <= prev:
            due = prev + timedelta(minutes=30)
            if apply_night:
                due = shift_into_window(due, tz_name, from_hour, to_hour)
        plan.append((step, due))
        prev = due
    return plan


def landing_link(base_url: str, anchor: str = "program", source: str | None = None) -> str:
    """Ссылка на лендинг: LANDING_URL?src=<метка слова>#program

    src помогает видеть в аналитике сайта, с какого видео пришёл человек.
    Существующие параметры в LANDING_URL сохраняются, якорь заменяется на anchor.
    """
    if not base_url:
        return ""
    parts = urlsplit(base_url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "src"]
    if source:
        query.append(("src", source))
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", urlencode(query), anchor.lstrip("#")))


# ---------- рассылка ----------
BUTTON_PREFIXES = ("кнопка:", "button:")
_URL_RE = re.compile(r"^(https?://|tg://)\S+$", re.IGNORECASE)


def parse_button_line(text: str | None) -> tuple[tuple[str, str] | None, bool]:
    """Ищет в ПОСЛЕДНЕЙ строке «Кнопка: Текст | https://...».

    Возвращает ((текст, ссылка) или None, найдена_ли_строка_кнопки).
    Если строка начинается с «Кнопка:», но формат неверный — ((None), True),
    чтобы бот подсказал, как правильно.
    """
    lines = (text or "").rstrip().splitlines()
    if not lines:
        return None, False
    last = lines[-1].strip()
    if not last.casefold().startswith(BUTTON_PREFIXES):
        return None, False
    body = last.split(":", 1)[1]
    if "|" not in body:
        return None, True
    label, url = (x.strip() for x in body.rsplit("|", 1))
    if not label or not _URL_RE.match(url):
        return None, True
    return (label[:64], url), True


def strip_last_line(text: str | None) -> str:
    lines = (text or "").rstrip().splitlines()
    return "\n".join(lines[:-1]).rstrip()
