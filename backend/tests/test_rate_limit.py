import pytest

from app.core.config import get_settings, parse_rate_limit
from app.core.rate_limit import check_rate_limit


def test_parse_rate_limit_variants():
    assert parse_rate_limit("10/minute") == (10, 60)
    assert parse_rate_limit("5/second") == (5, 1)
    assert parse_rate_limit("20/hours") == (20, 3600)
    with pytest.raises(ValueError):
        parse_rate_limit("10/fortnight")
    with pytest.raises(ValueError):
        parse_rate_limit("багато/minute")


async def test_check_rate_limit_counts_within_window(redis):
    for _ in range(3):
        allowed, retry = await check_rate_limit(
            redis, scope="test", identity="1.2.3.4", limit=3, window_seconds=60
        )
        assert allowed is True
        assert retry == 0

    allowed, retry = await check_rate_limit(
        redis, scope="test", identity="1.2.3.4", limit=3, window_seconds=60
    )
    assert allowed is False
    assert 0 < retry <= 60


async def test_login_rate_limit_returns_429(client, make_user):
    get_settings().rate_limit_login = "3/minute"
    await make_user()

    for _ in range(3):
        response = await client.post(
            "/api/v1/auth/login", json={"email": "admin@example.com", "password": "bad-password-1"}
        )
        assert response.status_code == 401

    limited = await client.post(
        "/api/v1/auth/login", json={"email": "admin@example.com", "password": "bad-password-1"}
    )
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "rate_limited"
    assert int(limited.headers["Retry-After"]) > 0


async def test_enroll_rate_limit(client, device_key):
    get_settings().rate_limit_enroll = "2/hour"

    for _ in range(2):
        await client.post(
            "/api/v1/enroll",
            json={
                "token": "FLEET-AAAA-BBBB-CCCC-DDDD-EEEE",
                "public_key": device_key.public_key_b64,
                "device": {"management_mode": "unmanaged"},
            },
        )

    limited = await client.post(
        "/api/v1/enroll",
        json={
            "token": "FLEET-AAAA-BBBB-CCCC-DDDD-EEEE",
            "public_key": device_key.public_key_b64,
            "device": {"management_mode": "unmanaged"},
        },
    )
    assert limited.status_code == 429


async def test_rate_limit_can_be_disabled(client, make_user):
    settings = get_settings()
    settings.rate_limit_login = "1/minute"
    settings.rate_limit_enabled = False
    await make_user()

    for _ in range(4):
        response = await client.post(
            "/api/v1/auth/login", json={"email": "admin@example.com", "password": "bad-password-1"}
        )
        assert response.status_code == 401
