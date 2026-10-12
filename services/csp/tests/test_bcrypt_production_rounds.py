"""守住正式 bcrypt cost:conftest 為了速度把測試行程降到 4,這裡確認原始碼仍是 12。"""
import bcrypt

from app.utils import security
from tests import conftest


def test_production_bcrypt_rounds_is_12():
    assert conftest._PRODUCTION_BCRYPT_ROUNDS == 12


def test_hash_uses_configured_rounds(monkeypatch):
    monkeypatch.setattr(security, "_BCRYPT_ROUNDS", 12)
    h = security.hash_password("pw")
    assert h.startswith("$2b$12$")
    assert security.verify_password("pw", h)


def test_low_cost_hash_still_verifies():
    h = security.hash_password("pw")
    assert h.startswith("$2b$04$") and bcrypt.checkpw(b"pw", h.encode())
