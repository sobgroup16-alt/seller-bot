"""Проверки чистой логики: команды, шаг согласий и разбор deep-link."""
from logic import (
    build_word_index,
    consent_stage,
    parse_code_word,
    parse_start_payload,
    telegram_command,
)

INDEX = build_word_index({
    "utechki": {"words": ["утечки", "утечка"]},
    "agent": {"words": ["агент", "агента"]},
})


def test_deep_links():
    assert parse_start_payload("utechki", INDEX) == "utechki"
    assert parse_start_payload("agent", INDEX) == "agent"
    assert parse_start_payload("UTECHKI", INDEX) == "utechki"
    assert parse_start_payload("утечки", INDEX) == "utechki"
    assert parse_start_payload(None, INDEX) is None
    assert parse_start_payload("other", INDEX) is None


def test_code_word_still_recognized():
    assert parse_code_word("УТЕЧКИ", INDEX) == "utechki"
    assert parse_code_word("агент!", INDEX) == "agent"
    assert parse_code_word("привет", INDEX) is None


def test_telegram_command():
    assert telegram_command("/stop") == ("/stop", None)
    assert telegram_command("/STOP") == ("/stop", None)
    assert telegram_command("/stop@ElenaBot") == ("/stop", None)
    assert telegram_command("/start utechki") == ("/start", "utechki")
    assert telegram_command("/start@seller_bot agent") == ("/start", "agent")
    assert telegram_command("/start") == ("/start", None)
    assert telegram_command("утечки") == (None, None)
    assert telegram_command(None) == (None, None)
    assert telegram_command("  /myid  ") == ("/myid", None)


def test_consent_stage():
    assert consent_stage(None, None) == "pd"
    assert consent_stage(0, None) == "pd"
    assert consent_stage(1, None) == "mailing"
    assert consent_stage(1, 0) == "done"
    assert consent_stage(1, 1) == "done"
    assert consent_stage(None, 1) == "pd"
