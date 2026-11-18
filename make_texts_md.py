"""Собирает «Тексты бота.md» из texts.yaml (чтобы читать весь сценарий подряд). Запуск: python make_texts_md.py"""
import re
from pathlib import Path

from texts import load_texts

t = load_texts()
b = t["buttons"]
a = t["admin"]


def show(s: str, **kw) -> str:
    s = s.strip()
    for k, v in kw.items():
        s = s.replace("{" + k + "}", v)
    s = re.sub(r"</?b>", "**", s)
    s = re.sub(r"</?i>", "_", s)
    s = re.sub(r"</?code>", "`", s)
    return "\n".join("> " + line if line else ">" for line in s.splitlines())


def btns(*labels) -> str:
    return "\n".join(f"> [ {l} ]" for l in labels)


src = t["sources"]
aud = a["bc_audiences"]
out = [
    "# Тексты бота — сценарий по порядку",
    "",
    "Это все сообщения бота в том порядке, в котором их видит человек. "
    "Править их нужно в файле `texts.yaml` (этот файл собирается из него автоматически: `python make_texts_md.py`).",
    "",
    "Вместо {name} бот подставляет имя человека из Telegram, здесь для примера «Анна». "
    "Кнопки показаны в квадратных скобках. ✍️ — нужно ваше решение или данные.",
    "",
    "Оплаты в боте нет: курс оплачивается на лендинге. Бот не знает, кто оплатил, поэтому сообщения получают все, "
    "кто не остановил бота.",
    "",
    "## 1. Человек пришёл без кодового слова (/start)",
    show(t["welcome"]), btns(src["utechki"]["button"], src["agent"]["button"]), "",
    "Если человек пишет что-то непонятное:", show(t["unknown"]),
    btns(src["utechki"]["button"], src["agent"]["button"]), "",
    "## 2. Кодовое слово УТЕЧКИ (или ссылка ?start=utechki)",
    show(src["utechki"]["intro"], name="Анна"), btns(t["watch_button"]),
    "_(если в настройках задан file_id, вместо кнопки приходит само видео с этим текстом)_", "",
    "## 3. Кодовое слово АГЕНТ (или ссылка ?start=agent)",
    show(src["agent"]["intro"], name="Анна"), btns(t["watch_button"]), "",
    "## 4. Сразу после видео (для обоих слов)",
    show(t["after_lead"], name="Анна"), btns(b["program"]),
    "_Кнопка открывает лендинг на блоке программы: ✍️ LANDING_URL?src=utechki#program (или src=agent)._", "",
    "## 5. Через 2 часа — приглашение в канал (CHANNEL_INVITE_DELAY_HOURS)",
    show(t["channel_invite"], name="Анна"), btns(b["channel"]),
    "_Кнопка ведёт на ✍️ CHANNEL_URL. Если ссылка не задана, сообщение не отправляется._", "",
    "## 6. Прогрев",
    "### Прогрев 1 — через сутки (WARMUP1_DELAY_HOURS)",
    show(t["warmup1"], name="Анна"), btns(b["program"]), "",
    "### Прогрев 2 — примерно через 2,5 дня (WARMUP2_DELAY_HOURS)",
    show(t["warmup2"], name="Анна"), btns(b["program"]), "",
    "Ночью бот не пишет: если время выпало на ночь, сообщение уйдёт в 10:00 по Москве (настраивается). "
    "Это касается каждого сообщения отдельно: приглашение через 2 часа сдвигается, только если само попало на ночь.", "",
    "## 7. Рассылка по базе (видите только вы)",
    "Вы пишете `/broadcast`, бот отвечает:", show(a["bc_ask"]), "",
    "Вы присылаете сообщение. Бот показывает его так, как увидят люди, и спрашивает, кому отправить:",
    show(a["bc_preview_title"]),
    show(a["bc_preview"], audience=aud["all"], count="120"),
    f"> [ ✅ {aud['all']} (120) ] [ {aud['utechki']} (70) ] [ {aud['agent']} (60) ]",
    f"> [ {b['bc_send']} ] [ {b['bc_cancel']} ]",
    "_(числа здесь для примера, бот покажет настоящие)_", "",
    "После «Отправить»:", show(a["bc_started"], audience=aud["all"], total="120", rate="15"), "",
    "Когда рассылка закончится:", show(a["bc_done"], total="120", sent="117", failed="0", blocked="3"),
    "_(числа для примера)_", "",
    "Если что-то не так:",
    show(a["bc_button_bad"]), "", show(a["bc_unsupported"]), "", show(a["bc_too_long"]), "",
    show(a["bc_busy"]), "", show(a["bc_cancelled"]), "",
    "## 8. Ваши команды",
    "- `/broadcast` — рассылка по базе (текст, фото или видео, по желанию с кнопкой-ссылкой).",
    "- `/cancel` — отменить рассылку, пока она не отправлена.",
    "- `/stats` — сколько людей пришло по каждому слову, сколько получили каждое отложенное сообщение, "
    "сколько остановили бота и сколько было рассылок.",
    "- `/myid` — показать ваш chat id (нужен для ADMIN_ID).",
    "- Пришлите боту видео — он ответит его file_id.",
    "",
    "Нажатия на «Посмотреть программу» бот не считает: это прямая ссылка на сайт. "
    "Переходы видно в аналитике сайта по метке src (utechki или agent).",
]
Path("Тексты бота.md").write_text("\n".join(out) + "\n", encoding="utf-8")
print("ok")
