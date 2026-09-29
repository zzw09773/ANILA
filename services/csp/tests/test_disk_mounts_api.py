"""儀表板磁碟磚：標籤、使用率、剩餘 GiB。不回宿主機路徑。"""
from __future__ import annotations

from app.services.alert_detectors import DiskSample, public_disk_mounts
from app.services.auth_service import create_tokens
from tests.conftest import make_user


def test_public_disk_mounts_omit_paths():
    sample = DiskSample(
        label="ingestion",
        path="/var/secret/uploads",
        used_pct=42.26,
        free_bytes=5 * (1024**3),
        total_bytes=40 * (1024**3),
    )
    rows = public_disk_mounts([sample])
    assert rows == [{"label": "ingestion", "used_pct": 42.3, "free_gib": 5.0}]
    assert "path" not in rows[0]
    assert "/var/secret" not in str(rows)


def _bearer(user) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_tokens(user)['access_token']}"}


def test_disk_mounts_api_requires_admin_and_hides_paths(client, db, monkeypatch):
    sample = DiskSample(
        label="attachments",
        path="/srv/private/attachments",
        used_pct=10.0,
        free_bytes=2 * (1024**3),
        total_bytes=20 * (1024**3),
    )
    monkeypatch.setattr("app.api.admin.capacity.current_disk_mounts", lambda: [sample])
    assert client.get("/api/admin/disk-mounts").status_code == 401
    user = make_user(db, username="disk-user", role="user")
    assert client.get("/api/admin/disk-mounts", headers=_bearer(user)).status_code == 403
    admin = make_user(db, username="disk-admin", role="admin")
    denied = client.get("/api/admin/disk-mounts?path=/", headers=_bearer(admin))
    assert denied.status_code == 400
    response = client.get("/api/admin/disk-mounts", headers=_bearer(admin))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body == [{"label": "attachments", "used_pct": 10.0, "free_gib": 2.0}]
    assert "/srv/private" not in response.text
