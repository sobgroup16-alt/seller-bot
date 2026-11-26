"""Тонкий клиент MAX Bot API.

Документация, на которую опирается этот модуль:
- https://dev.max.ru/docs-api/methods/GET/updates
- https://dev.max.ru/docs-api/methods/POST/messages
- https://dev.max.ru/docs-api/methods/POST/answers
- https://dev.max.ru/docs-api/use-cases/sending-messages/keyboard
- https://dev.max.ru/docs-api/

Токен передаётся только заголовком Authorization. В query его класть нельзя:
платформа это больше не принимает.
"""
from __future__ import annotations

import asyncio
import logging
import ssl
from pathlib import Path
from typing import Any, Awaitable, Callable

import certifi

log = logging.getLogger("max_api")

API_BASE = "https://platform-api2.max.ru"
# Сертификаты Минцифры с Госуслуг (gu-st.ru). Их нет в наборе certifi,
# без них aiohttp не может проверить TLS platform-api2.max.ru.
# Отпечатки SHA-256 сверены при добавлении в репозиторий.
CERT_DIR = Path(__file__).resolve().parent / "certs"
RUSSIAN_CA_FILES = (
    "russian_trusted_root_ca.pem",       # Russian Trusted Root CA, до 2032-02-27
    "russian_trusted_sub_ca.pem",         # Russian Trusted Sub CA, 2022–2027
    "russian_trusted_sub_ca_2024.pem",    # Russian Trusted Sub CA, выпускает *.max.ru
)
# Официально: не больше 30 запросов в секунду на platform-api2.max.ru
# и не больше 2 сообщений в секунду в один диалог.
MAX_RPS = 30

# Коды из общего описания API: диалог закрыт, пользователь остановил бота, чата нет.
BLOCK_CODES = frozenset({
    "chat.denied",
    "dialog.suspended",
    "chat.not.found",
    "user.not.found",
    "not.found",
})

Transport = Callable[
    [str, str, dict | None, dict | None, dict],
    Awaitable[tuple[int, Any]],
]


class MaxApiError(Exception):
    def __init__(self, status: int, code: str, message: str, retry_after: float | None = None):
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.retry_after = retry_after


def is_blocked(err: MaxApiError) -> bool:
    return err.status in (403, 404) or err.code in BLOCK_CODES


def is_retryable(err: MaxApiError) -> bool:
    return err.status == 429 or err.code in {"too.many.requests", "rate.limit"} or err.status >= 500


def callback_button(text: str, payload: str) -> dict:
    """Кнопка callback. Текст и payload — до 128 символов по документации клавиатуры."""
    return {"type": "callback", "text": text[:128], "payload": payload[:128]}


def link_button(text: str, url: str) -> dict:
    """Кнопка-ссылка. URL до 2048 символов, в одном ряду таких кнопок не больше трёх."""
    return {"type": "link", "text": text[:128], "url": url[:2048]}


def inline_keyboard(rows: list[list[dict]]) -> list[dict]:
    return [{"type": "inline_keyboard", "payload": {"buttons": rows}}]


def build_ssl_context() -> ssl.SSLContext:
    """Доверие certifi плюс корневой и промежуточные УЦ Минцифры. Проверка не отключается."""
    context = ssl.create_default_context(cafile=certifi.where())
    for name in RUSSIAN_CA_FILES:
        context.load_verify_locations(cafile=str(CERT_DIR / name))
    context.verify_mode = ssl.CERT_REQUIRED
    context.check_hostname = True
    return context


class MaxClient:
    def __init__(self, token: str, *, base_url: str = API_BASE, transport: Transport | None = None):
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.transport = transport
        self.ssl_context = None if transport is not None else build_ssl_context()
        self._session = None

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def request(self, method: str, path: str, params: dict | None = None,
                      body: dict | None = None) -> Any:
        headers = {"Authorization": self.token}
        if self.transport is not None:
            status, data = await self.transport(method, path, params, body, headers)
        else:
            status, data = await self._http(method, path, params, body, headers)
        return self._interpret(status, data)

    async def _http(self, method: str, path: str, params: dict | None, body: dict | None,
                    headers: dict) -> tuple[int, Any]:
        import aiohttp

        # Дольше long polling (до 90 с по документации), одна сессия на все запросы.
        timeout = aiohttp.ClientTimeout(total=120)
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(ssl=self.ssl_context or build_ssl_context())
            self._session = aiohttp.ClientSession(
                timeout=timeout, connector=connector, connector_owner=True,
            )
        url = self.base_url + path
        req_headers = dict(headers)
        if body is not None:
            req_headers["Content-Type"] = "application/json"
        # types передаётся повторяющимся query-параметром, не строкой через запятую.
        query = []
        for key, value in (params or {}).items():
            if value is None:
                continue
            if isinstance(value, (list, tuple)):
                for item in value:
                    query.append((key, str(item)))
            else:
                query.append((key, str(value)))
        async with self._session.request(method, url, params=query, json=body, headers=req_headers) as resp:
            try:
                data = await resp.json(content_type=None)
            except Exception:  # noqa: BLE001
                data = {"message": await resp.text()}
            return resp.status, data

    @staticmethod
    def _interpret(status: int, data: Any) -> Any:
        if not isinstance(data, dict):
            if status >= 400:
                raise MaxApiError(status, "", str(data))
            return data
        code = str(data.get("code") or "")
        message = str(data.get("message") or "")
        if data.get("success") is False or status >= 400:
            retry = data.get("retry_after")
            try:
                retry_after = float(retry) if retry is not None else None
            except (TypeError, ValueError):
                retry_after = None
            raise MaxApiError(status if status >= 400 else 400, code, message, retry_after)
        return data

    async def get_me(self) -> dict:
        data = await self.request("GET", "/me")
        return data if isinstance(data, dict) else {}

    async def get_subscriptions(self) -> list:
        data = await self.request("GET", "/subscriptions")
        if isinstance(data, dict):
            return list(data.get("subscriptions") or [])
        return []

    async def get_updates(self, *, marker: int | None = None, timeout: int = 25,
                          limit: int = 100, types: list[str] | None = None) -> dict:
        params: dict[str, Any] = {"timeout": timeout, "limit": limit}
        if marker is not None:
            params["marker"] = marker
        if types:
            params["types"] = types
        data = await self.request("GET", "/updates", params=params)
        return data if isinstance(data, dict) else {"updates": [], "marker": marker}

    async def send_message(self, user_id: int, text: str, attachments: list | None = None) -> dict:
        # disable_link_preview — query-параметр POST /messages, не поле тела.
        params = {"user_id": user_id, "disable_link_preview": "true"}
        body: dict[str, Any] = {"text": text[:4000], "format": "html"}
        if attachments is not None:
            body["attachments"] = attachments
        data = await self.request("POST", "/messages", params=params, body=body)
        return data if isinstance(data, dict) else {}

    async def answer_callback(self, callback_id: str, *, notification: str = "") -> dict:
        """Снимает «часики» у кнопки. Пустое уведомление не заменяет текст сообщения."""
        data = await self.request(
            "POST", "/answers",
            params={"callback_id": callback_id},
            body={"notification": notification},
        )
        return data if isinstance(data, dict) else {}


async def sleep_retry(err: MaxApiError) -> None:
    await asyncio.sleep((err.retry_after or 1) + 0.2)
