"""TLS к MAX: доверие certifi плюс УЦ Минцифры, проверка не отключается."""
from __future__ import annotations

import asyncio
import hashlib
import ssl
from pathlib import Path
from unittest.mock import patch

import pytest

from config import Settings
from max_api import CERT_DIR, RUSSIAN_CA_FILES, MaxClient, build_ssl_context

# Отпечатки SHA-256 файлов с https://gu-st.ru (Госуслуги), сверены при добавлении.
FINGERPRINTS = {
    "russian_trusted_root_ca.pem": "d26d2d0231b7c39f92cc738512ba54103519e4405d68b5bd703e9788ca8ecf31",
    "russian_trusted_sub_ca.pem": "bbbde2103e790b999ec62bd03cf625a5a2e7c316e10afe6a490eedead8b3fd9b",
    "russian_trusted_sub_ca_2024.pem": "2155785036c900dbb5f1bb2a1569c80c55595bd6bf94867a29bbddbc7d88a3f2",
}


def _fingerprint(path: Path) -> str:
    der = ssl.PEM_cert_to_DER_cert(path.read_text(encoding="utf-8"))
    return hashlib.sha256(der).hexdigest()


def test_bundled_cas_match_official_fingerprints():
    assert set(RUSSIAN_CA_FILES) == set(FINGERPRINTS)
    for name, expected in FINGERPRINTS.items():
        path = CERT_DIR / name
        assert path.is_file(), name
        assert _fingerprint(path) == expected


def test_ssl_context_requires_verification_and_trusts_russian_cas():
    context = build_ssl_context()
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    names = set()
    for cert in context.get_ca_certs():
        for pair in cert["subject"]:
            if pair[0][0] == "commonName":
                names.add(pair[0][1])
    assert "Russian Trusted Root CA" in names
    assert "Russian Trusted Sub CA" in names


def test_client_uses_that_context_only_for_real_http():
    mocked = MaxClient("token", transport=lambda *args: None)
    assert mocked.ssl_context is None
    real = MaxClient("token")
    assert real.ssl_context is not None
    assert real.ssl_context.verify_mode == ssl.CERT_REQUIRED
    assert real.ssl_context.check_hostname is True


def test_startup_error_closes_the_session(tmp_path):
    async def run():
        closed = []

        class API:
            def __init__(self, token):
                self.token = token

            async def get_me(self):
                raise ssl.SSLCertVerificationError("unable to get local issuer certificate")

            async def close(self):
                closed.append(self.token)

        cfg = Settings(
            bot_token="tg-not-used",
            admin_ids=[],
            lead_utechki_url="",
            lead_utechki_file_id="",
            lead_agent_url="",
            lead_agent_file_id="",
            channel_url="",
            landing_url="",
            program_delay_minutes=10,
            channel_invite_delay_hours=2,
            warmup1_delay_hours=24,
            warmup2_delay_hours=60,
            timezone="Europe/Moscow",
            send_from_hour=10,
            send_to_hour=21,
            scheduler_interval_seconds=60,
            max_bot_token="max-secret",
            max_db_path=tmp_path / "max.sqlite3",
        )
        with patch("max_bot.MaxClient", API), patch("max_bot.load_settings", lambda require_token=False: cfg):
            with pytest.raises(ssl.SSLCertVerificationError):
                from max_bot import main
                await main()
        assert closed == ["max-secret"]

    asyncio.run(run())
