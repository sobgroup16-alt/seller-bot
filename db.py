"""База данных SQLite: пользователи, события, очередь отложенных сообщений и журнал рассылок.

Очередь хранится в базе, поэтому после перезапуска бота ничего не теряется.
Оплату бот не отслеживает: курс оплачивается на лендинге.
Прогрев и рассылку получают только те, кто согласился на рассылку и не остановил бота.
"""
from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id     INTEGER PRIMARY KEY,
    username    TEXT,
    full_name   TEXT,
    source      TEXT,              -- первое кодовое слово, с которым пришёл человек
    last_source TEXT,              -- последнее кодовое слово
    created_at  INTEGER NOT NULL,
    blocked     INTEGER NOT NULL DEFAULT 0,  -- 1 = человек остановил бота, ему не пишем
    pd_consent         INTEGER,   -- 1 = согласие на обработку персональных данных
    pd_consent_at      INTEGER,   -- unix UTC, когда согласился (первый раз, не перезаписывается)
    mailing_consent    INTEGER,   -- 1 = можно слать рассылку, 0 = отказ или /stop, NULL = ещё не ответил
    mailing_consent_at INTEGER,   -- unix UTC, когда принято текущее решение по рассылке
    pending_source     TEXT       -- utechki / agent, параметр запуска до конца согласий
);
CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    kind       TEXT NOT NULL,      -- start, lead_*, after_lead, channel_invite, warmup*, pd_consent, mailing_yes, mailing_no, mailing_stop
    source     TEXT,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS scheduled (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id   INTEGER NOT NULL,
    step      TEXT NOT NULL,       -- after_lead, channel_invite, warmup1, warmup2
    due_at    INTEGER NOT NULL,    -- время отправки (unix, UTC)
    sent_at   INTEGER,
    cancelled INTEGER NOT NULL DEFAULT 0,
    UNIQUE(user_id, step)
);
CREATE INDEX IF NOT EXISTS idx_sched_due ON scheduled(due_at) WHERE sent_at IS NULL AND cancelled = 0;
CREATE TABLE IF NOT EXISTS broadcasts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_id    INTEGER NOT NULL,
    audience    TEXT NOT NULL,     -- all, utechki, agent
    kind        TEXT NOT NULL,     -- text, photo, video
    preview     TEXT,              -- начало текста, для истории
    total       INTEGER NOT NULL DEFAULT 0,
    sent        INTEGER NOT NULL DEFAULT 0,
    failed      INTEGER NOT NULL DEFAULT 0,
    blocked     INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'running',   -- running, done, error
    created_at  INTEGER NOT NULL,
    finished_at INTEGER
);
"""

AUDIENCES = ("all", "utechki", "agent")
# Приглашение в канал и прогрев — рекламные сообщения. Сообщение с программой (after_lead)
# к ним не относится: это продолжение выдачи материала через 10 минут.
PROMO_STEPS = ("channel_invite", "warmup1", "warmup2")


def now_ts() -> int:
    return int(time.time())


def to_ts(dt: datetime) -> int:
    return int(dt.timestamp())


def from_ts(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc)


class DB:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self._ensure_user_columns()
        self.conn.commit()

    def _ensure_user_columns(self) -> None:
        """Старые базы (до экрана согласий) получают новые поля без потери людей."""
        have = {row[1] for row in self.conn.execute("PRAGMA table_info(users)")}
        adds = (
            ("pd_consent", "ALTER TABLE users ADD COLUMN pd_consent INTEGER"),
            ("pd_consent_at", "ALTER TABLE users ADD COLUMN pd_consent_at INTEGER"),
            ("mailing_consent", "ALTER TABLE users ADD COLUMN mailing_consent INTEGER"),
            ("mailing_consent_at", "ALTER TABLE users ADD COLUMN mailing_consent_at INTEGER"),
            ("pending_source", "ALTER TABLE users ADD COLUMN pending_source TEXT"),
        )
        for name, sql in adds:
            if name not in have:
                self.conn.execute(sql)

    # ---------- пользователи ----------
    def upsert_user(self, user_id: int, username: str | None, full_name: str | None,
                    source: str | None = None, ts: int | None = None) -> None:
        ts = ts or now_ts()
        self.conn.execute(
            """INSERT INTO users(user_id, username, full_name, source, last_source, created_at)
               VALUES(?,?,?,?,?,?)
               ON CONFLICT(user_id) DO UPDATE SET
                 username=excluded.username,
                 full_name=excluded.full_name,
                 source=COALESCE(users.source, excluded.source),
                 last_source=COALESCE(excluded.last_source, users.last_source),
                 blocked=0""",
            (user_id, username, full_name, source, source, ts),
        )
        self.conn.commit()

    def ensure_user(self, user_id: int, username: str | None, full_name: str | None,
                    ts: int | None = None) -> None:
        """Создаёт человека, если его ещё нет. Не снимает блокировку и не меняет источник."""
        ts = ts or now_ts()
        self.conn.execute(
            """INSERT INTO users(user_id, username, full_name, created_at)
               VALUES(?,?,?,?)
               ON CONFLICT(user_id) DO UPDATE SET
                 username=excluded.username,
                 full_name=excluded.full_name""",
            (user_id, username, full_name, ts),
        )
        self.conn.commit()

    def get_user(self, user_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()

    def set_pd_consent(self, user_id: int, ts: int | None = None) -> None:
        """Согласие на ПДн. Время первого согласия не перезаписывается."""
        ts = ts or now_ts()
        self.conn.execute(
            """UPDATE users SET pd_consent=1, pd_consent_at=COALESCE(pd_consent_at, ?)
               WHERE user_id=?""",
            (ts, user_id),
        )
        self.conn.commit()

    def set_mailing_consent(self, user_id: int, agreed: bool, ts: int | None = None) -> None:
        """Текущее решение по рассылке и время этого решения.

        Согласие (agreed=True) заодно снимает блокировку: человек сам попросил писать ему.
        Отказ и /stop блокировку не ставят — материалы и ответы бота остаются.
        """
        ts = ts or now_ts()
        if agreed:
            self.conn.execute(
                "UPDATE users SET mailing_consent=1, mailing_consent_at=?, blocked=0 WHERE user_id=?",
                (ts, user_id),
            )
        else:
            self.conn.execute(
                "UPDATE users SET mailing_consent=0, mailing_consent_at=? WHERE user_id=?",
                (ts, user_id),
            )
        self.conn.commit()

    def mailing_allowed(self, user_id: int) -> bool:
        row = self.get_user(user_id)
        return bool(row and row["mailing_consent"] == 1)

    def set_pending_source(self, user_id: int, source: str) -> None:
        self.conn.execute("UPDATE users SET pending_source=? WHERE user_id=?", (source, user_id))
        self.conn.commit()

    def get_pending_source(self, user_id: int) -> str | None:
        row = self.get_user(user_id)
        if not row:
            return None
        return row["pending_source"]

    def clear_pending_source(self, user_id: int) -> None:
        self.conn.execute("UPDATE users SET pending_source=NULL WHERE user_id=?", (user_id,))
        self.conn.commit()

    def has_event(self, user_id: int, kind: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM events WHERE user_id=? AND kind=? LIMIT 1", (user_id, kind)
        ).fetchone() is not None

    def has_lead(self, user_id: int) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM events WHERE user_id=? AND kind LIKE 'lead_%' LIMIT 1", (user_id,)
        ).fetchone() is not None

    def mark_blocked(self, user_id: int) -> None:
        self.conn.execute("UPDATE users SET blocked=1 WHERE user_id=?", (user_id,))
        self.cancel_pending(user_id)

    # ---------- события ----------
    def log_event(self, user_id: int, kind: str, source: str | None = None, ts: int | None = None) -> None:
        self.conn.execute(
            "INSERT INTO events(user_id, kind, source, created_at) VALUES(?,?,?,?)",
            (user_id, kind, source, ts or now_ts()),
        )
        self.conn.commit()

    # ---------- очередь отложенных сообщений ----------
    def has_schedule(self, user_id: int) -> bool:
        return self.conn.execute("SELECT 1 FROM scheduled WHERE user_id=? LIMIT 1", (user_id,)).fetchone() is not None

    def schedule(self, user_id: int, plan: list[tuple[str, datetime]]) -> None:
        self.conn.executemany(
            "INSERT OR IGNORE INTO scheduled(user_id, step, due_at) VALUES(?,?,?)",
            [(user_id, step, to_ts(due)) for step, due in plan],
        )
        self.conn.commit()

    def reschedule(self, user_id: int, step: str, due: datetime) -> None:
        """Ставит или заново ставит один шаг (сбрасывает sent_at и cancelled)."""
        self.conn.execute(
            """INSERT INTO scheduled(user_id, step, due_at, sent_at, cancelled)
               VALUES(?,?,?,NULL,0)
               ON CONFLICT(user_id, step) DO UPDATE SET
                 due_at=excluded.due_at, sent_at=NULL, cancelled=0""",
            (user_id, step, to_ts(due)),
        )
        self.conn.commit()

    def cancel_pending(self, user_id: int) -> None:
        self.conn.execute(
            "UPDATE scheduled SET cancelled=1 WHERE user_id=? AND sent_at IS NULL", (user_id,)
        )
        self.conn.commit()

    def cancel_promo(self, user_id: int) -> None:
        """Снимает ещё не отправленные прогрев и приглашение в канал. Программу курса не трогает."""
        marks = ",".join("?" * len(PROMO_STEPS))
        self.conn.execute(
            f"""UPDATE scheduled SET cancelled=1
                WHERE user_id=? AND sent_at IS NULL AND step IN ({marks})""",
            (user_id, *PROMO_STEPS),
        )
        self.conn.commit()

    def due_messages(self, ts: int | None = None, limit: int = 50) -> list[sqlite3.Row]:
        """Сообщения, которым пора уйти.

        Тем, кто остановил бота, не отдаём. Прогрев и приглашение в канал не отдаём,
        пока mailing_consent не равен 1. Такие строки не занимают лимит выборки:
        иначе очередь из несогласившихся остановила бы сообщение с программой.
        """
        marks = ",".join("?" * len(PROMO_STEPS))
        return self.conn.execute(
            f"""SELECT s.id, s.user_id, s.step, u.source, u.last_source
               FROM scheduled s JOIN users u ON u.user_id = s.user_id
               WHERE s.sent_at IS NULL AND s.cancelled = 0 AND s.due_at <= ?
                 AND u.blocked = 0
                 AND (s.step NOT IN ({marks}) OR u.mailing_consent = 1)
               ORDER BY s.due_at, s.id LIMIT ?""",
            (ts or now_ts(), *PROMO_STEPS, limit),
        ).fetchall()

    def mark_sent(self, sched_id: int, ts: int | None = None) -> None:
        self.conn.execute("UPDATE scheduled SET sent_at=? WHERE id=?", (ts or now_ts(), sched_id))
        self.conn.commit()

    def cancel_one(self, sched_id: int) -> None:
        self.conn.execute("UPDATE scheduled SET cancelled=1 WHERE id=?", (sched_id,))
        self.conn.commit()

    # ---------- рассылка ----------
    def broadcast_recipients(self, audience: str = "all") -> list[int]:
        """Кому уйдёт рассылка: согласие на рассылку есть, бота не остановил.

        Старые пользователи без mailing_consent сюда не попадают.
        utechki / agent — те, кто хоть раз получал это видео (человек, взявший оба, есть в обоих).
        """
        if audience == "all":
            rows = self.conn.execute(
                """SELECT user_id FROM users
                   WHERE blocked=0 AND mailing_consent=1
                   ORDER BY created_at, user_id"""
            )
        elif audience in AUDIENCES:
            rows = self.conn.execute(
                """SELECT u.user_id FROM users u
                   WHERE u.blocked=0 AND u.mailing_consent=1 AND (u.source=? OR u.last_source=? OR EXISTS(
                       SELECT 1 FROM events e WHERE e.user_id=u.user_id AND e.kind=?))
                   ORDER BY u.created_at, u.user_id""",
                (audience, audience, f"lead_{audience}"),
            )
        else:
            raise ValueError(f"Неизвестная аудитория: {audience}")
        return [r[0] for r in rows.fetchall()]

    def create_broadcast(self, admin_id: int, audience: str, kind: str, preview: str, total: int,
                         ts: int | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO broadcasts(admin_id, audience, kind, preview, total, created_at) VALUES(?,?,?,?,?,?)",
            (admin_id, audience, kind, (preview or "")[:200], total, ts or now_ts()),
        )
        self.conn.commit()
        return cur.lastrowid

    def finish_broadcast(self, bc_id: int, sent: int, failed: int, blocked: int, status: str = "done",
                         ts: int | None = None) -> None:
        self.conn.execute(
            "UPDATE broadcasts SET sent=?, failed=?, blocked=?, status=?, finished_at=? WHERE id=?",
            (sent, failed, blocked, status, ts or now_ts(), bc_id),
        )
        self.conn.commit()

    def get_broadcast(self, bc_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM broadcasts WHERE id=?", (bc_id,)).fetchone()

    # ---------- статистика ----------
    def stats(self) -> dict:
        c = self.conn
        users_total = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        by_source = c.execute(
            "SELECT COALESCE(source,'без слова') s, COUNT(*) n FROM users GROUP BY s ORDER BY n DESC"
        ).fetchall()
        leads = c.execute(
            "SELECT kind, COUNT(DISTINCT user_id) u FROM events WHERE kind LIKE 'lead_%' GROUP BY kind"
        ).fetchall()
        sent = c.execute(
            "SELECT step, COUNT(*) FROM scheduled WHERE sent_at IS NOT NULL GROUP BY step"
        ).fetchall()
        blocked = c.execute("SELECT COUNT(*) FROM users WHERE blocked=1").fetchone()[0]
        bc_count = c.execute("SELECT COUNT(*) FROM broadcasts").fetchone()[0]
        bc_last = c.execute("SELECT * FROM broadcasts ORDER BY id DESC LIMIT 1").fetchone()
        return {
            "users_total": users_total,
            "by_source": [dict(r) for r in by_source],
            "leads": {r["kind"][5:]: r["u"] for r in leads},
            "sent": {r[0]: r[1] for r in sent},
            "blocked": blocked,
            "broadcasts": bc_count,
            "broadcast_last": dict(bc_last) if bc_last else None,
        }
