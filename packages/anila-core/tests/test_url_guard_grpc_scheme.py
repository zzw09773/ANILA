"""Additive ``grpc`` / ``grpcs`` scheme branch — must not alter http behaviour.

Red line: scheme check stays an if/elif on ``parsed.scheme``, gated by its
own kind/flag. Collapsing into a shared allow-list set is forbidden.
"""
from __future__ import annotations

import pytest

from anila_core.security import (
    ENDPOINT_KIND_AGENT,
    ENDPOINT_KIND_GENERIC,
    ENDPOINT_KIND_MODEL,
    UnsafeEndpointError,
    validate_outbound_url,
)

_HTTP_MODEL = "ANILA_ALLOW_HTTP_ENDPOINT"
_HTTP_AGENT = "ANILA_ALLOW_HTTP_AGENT_ENDPOINT"
_GRPC_MODEL = "ANILA_ALLOW_GRPC_ENDPOINT"
_PRIVATE = "ANILA_ALLOW_PRIVATE_ENDPOINT"
_PUBLIC_HTTP = "http://api.example.com/v1"
_PUBLIC_HTTPS = "https://api.example.com/v1"
_GRPC = "grpc://embed.example.com:9001"
_GRPCS = "grpcs://embed.example.com:9001"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in (
        _HTTP_MODEL,
        _HTTP_AGENT,
        _GRPC_MODEL,
        _PRIVATE,
        "ANILA_ENV",
        "ANILA_TRUSTED_HOSTS",
    ):
        monkeypatch.delenv(var, raising=False)
    yield


# ── http behaviour unchanged ────────────────────────────────────────────────

def test_model_http_still_rejected_without_flag():
    with pytest.raises(UnsafeEndpointError) as exc:
        validate_outbound_url(_PUBLIC_HTTP, endpoint_kind=ENDPOINT_KIND_MODEL)
    assert "ANILA_ALLOW_HTTP_ENDPOINT" in str(exc.value)


def test_model_http_still_ok_with_flag(monkeypatch):
    monkeypatch.setenv(_HTTP_MODEL, "1")
    validate_outbound_url(_PUBLIC_HTTP, endpoint_kind=ENDPOINT_KIND_MODEL)


def test_grpc_flag_does_not_admit_http(monkeypatch):
    """ANILA_ALLOW_GRPC_ENDPOINT must not widen ordinary http:// reach."""
    monkeypatch.setenv(_GRPC_MODEL, "1")
    with pytest.raises(UnsafeEndpointError):
        validate_outbound_url(_PUBLIC_HTTP, endpoint_kind=ENDPOINT_KIND_MODEL)


def test_http_flag_does_not_admit_grpc(monkeypatch):
    monkeypatch.setenv(_HTTP_MODEL, "1")
    with pytest.raises(UnsafeEndpointError):
        validate_outbound_url(_GRPC, endpoint_kind=ENDPOINT_KIND_MODEL)


# ── grpc / grpcs ────────────────────────────────────────────────────────────

def test_model_grpcs_ok_without_cleartext_flag(monkeypatch):
    monkeypatch.setenv(_PRIVATE, "1")  # example.com may resolve private in CI
    # Use a hostname that won't hit private-IP after DNS, or allow private.
    validate_outbound_url(_GRPCS, endpoint_kind=ENDPOINT_KIND_MODEL)


def test_model_grpc_requires_flag(monkeypatch):
    with pytest.raises(UnsafeEndpointError) as exc:
        validate_outbound_url(_GRPC, endpoint_kind=ENDPOINT_KIND_MODEL)
    assert "ANILA_ALLOW_GRPC_ENDPOINT" in str(exc.value)


def test_model_grpc_ok_with_flag(monkeypatch):
    monkeypatch.setenv(_GRPC_MODEL, "1")
    monkeypatch.setenv(_PRIVATE, "1")
    validate_outbound_url(_GRPC, endpoint_kind=ENDPOINT_KIND_MODEL)


def test_agent_grpc_rejected_even_with_flag(monkeypatch):
    monkeypatch.setenv(_GRPC_MODEL, "1")
    with pytest.raises(UnsafeEndpointError):
        validate_outbound_url(_GRPC, endpoint_kind=ENDPOINT_KIND_AGENT)


def test_generic_grpc_rejected(monkeypatch):
    monkeypatch.setenv(_GRPC_MODEL, "1")
    with pytest.raises(UnsafeEndpointError):
        validate_outbound_url(_GRPC, endpoint_kind=ENDPOINT_KIND_GENERIC)


def test_ftp_still_rejected():
    with pytest.raises(UnsafeEndpointError):
        validate_outbound_url("ftp://api.example.com/v1", endpoint_kind=ENDPOINT_KIND_MODEL)


def test_https_still_ok():
    validate_outbound_url(_PUBLIC_HTTPS, endpoint_kind=ENDPOINT_KIND_MODEL)


# ── SSRF 結構性封鎖對 grpc:// 一樣成立 ────────────────────────────────────────
#
# 上面那些測試證明的是「旗標控制得住 scheme」。它們證明不了的是:**旗標打開
# 之後,SSRF 的結構性封鎖還在不在**。新增一條 scheme 分支最典型的失敗形態不是
# 「忘了加旗標」,而是「旗標過了就直接放行,底下的 loopback / link-local /
# metadata 檢查被繞過去」——那正好把 grpc:// 變成打進宿主機與雲端 metadata 的
# 洞。這裡每一支都把旗標開到**最寬**(GRPC=1、PRIVATE=1、目標主機還放進
# TRUSTED_HOSTS),然後要求它照樣被擋。

_STRUCTURALLY_UNSAFE_HOSTS = [
    ("127.0.0.1:9001", "IPv4 loopback"),
    ("[::1]:9001", "IPv6 loopback"),
    ("localhost:9001", "loopback 名稱"),
    ("ip6-localhost:9001", "docker 的 IPv6 loopback 別名"),
    ("169.254.169.254:80", "雲端 metadata"),
    ("metadata.google.internal:9001", "GCP metadata 名稱"),
    ("metadata:9001", "metadata 短名"),
    ("host.docker.internal:9001", "Docker host gateway"),
    ("169.254.10.5:9001", "link-local"),
    ("224.0.0.1:9001", "multicast"),
    ("0.0.0.0:9001", "unspecified"),
    ("240.0.0.1:9001", "reserved"),
]
# ⚠ ``*.svc.cluster.local`` / ``*.internal`` 這類**內部域**故意不在上面。
# url_guard 把它們歸在 ``FIXABLE_BY_TRUST_HOST``——管理者把 docker/k8s 服務
# 名加進信任清單就能放行,那是既有設計(url_guard.py 的 internal-zone 分支
# 在 trusted 時 return)。硬要求它「連信任清單都解不開」等於偷偷改政策。
# 它的正確測法在 ``test_grpc_internal_zone_is_admin_fixable_not_structural``。


@pytest.mark.parametrize("scheme", ["grpc", "grpcs"], ids=["cleartext", "tls"])
@pytest.mark.parametrize(
    "hostport,why",
    _STRUCTURALLY_UNSAFE_HOSTS,
    ids=[h.split(":")[0].strip("[]") for h, _ in _STRUCTURALLY_UNSAFE_HOSTS],
)
def test_grpc_never_reaches_loopback_linklocal_or_metadata(
    monkeypatch, scheme, hostport, why
):
    """旗標開到最寬也不得放行 loopback / link-local / metadata / 內部域。

    ``grpcs`` 也要測:它**不需要**旗標,所以如果結構性檢查被寫進旗標分支
    底下,``grpcs`` 會是先漏的那一邊。
    """
    monkeypatch.setenv(_GRPC_MODEL, "1")
    monkeypatch.setenv(_PRIVATE, "1")
    # 連 trusted-hosts 都放行:管理者的 allow-list 也不該能解開這幾類。
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", hostport.rsplit(":", 1)[0].strip("[]"))
    with pytest.raises(UnsafeEndpointError) as exc:
        validate_outbound_url(
            f"{scheme}://{hostport}", endpoint_kind=ENDPOINT_KIND_MODEL
        )
    # 一定要對 reason 斷言。只寫 ``pytest.raises(UnsafeEndpointError)`` 的話,
    # 「scheme 被擋掉」也算通過 —— 那樣這批測試就完全沒有在測主機檢查,
    # 只要有人把 grpc 分支整個關掉,它們反而會全綠。
    assert exc.value.reason in ("deny_host", "unsafe_ip"), (
        f"{why} 被擋掉的理由是 {exc.value.reason!r},不是主機層的結構性封鎖"
    )


@pytest.mark.parametrize("scheme", ["grpc", "grpcs"])
@pytest.mark.parametrize(
    "hostport", ["127.0.0.1:9001", "169.254.169.254:80", "localhost:9001"]
)
def test_grpc_structural_rejects_are_not_fixable_by_trust_host(
    monkeypatch, scheme, hostport
):
    """loopback / metadata 的拒絕理由不得被標成「加進信任清單就能修」。

    ``fixable_by_trust_host`` 會直接變成治理中心給管理者的建議文案。對這幾類
    位址提示「要不要把它加進信任主機?」等於請管理者親手開一個 SSRF。
    """
    monkeypatch.setenv(_GRPC_MODEL, "1")
    with pytest.raises(UnsafeEndpointError) as exc:
        validate_outbound_url(
            f"{scheme}://{hostport}", endpoint_kind=ENDPOINT_KIND_MODEL
        )
    assert exc.value.fixable_by_trust_host is False
    assert exc.value.reason in ("deny_host", "unsafe_ip")


@pytest.mark.parametrize("scheme", ["grpc", "grpcs"])
def test_grpc_private_ip_still_needs_the_private_flag(monkeypatch, scheme):
    """RFC1918 對 grpc:// 仍受 ANILA_ALLOW_PRIVATE_ENDPOINT 管。

    這條與上面那批相反:內網 Triton **就是**坐在 RFC1918 上,所以它必須是
    「旗標可開」而不是「永遠擋」。兩者分屬不同類別,不能混為一談——真混了,
    要嘛內網用不了,要嘛 loopback 被放行。
    """
    monkeypatch.setenv(_GRPC_MODEL, "1")
    with pytest.raises(UnsafeEndpointError) as exc:
        validate_outbound_url(
            f"{scheme}://172.16.0.9:9001", endpoint_kind=ENDPOINT_KIND_MODEL
        )
    assert exc.value.reason == "private_ip"

    monkeypatch.setenv(_PRIVATE, "1")
    validate_outbound_url(
        f"{scheme}://172.16.0.9:9001", endpoint_kind=ENDPOINT_KIND_MODEL
    )


@pytest.mark.parametrize("scheme", ["grpc", "grpcs"])
def test_grpc_internal_zone_is_admin_fixable_not_structural(monkeypatch, scheme):
    """內部域(``.svc.cluster.local`` 等)預設擋、加進信任清單可放行。

    這是刻意與 loopback / metadata **分開**的類別。內網真的會有
    ``triton.svc.cluster.local`` 這種端點,所以它必須留一條管理者自救的路;
    而 loopback / metadata 沒有任何合法用途,信任清單也不該解得開。
    把這兩類混成同一個判斷,不論往哪邊倒都是錯的。
    """
    monkeypatch.setenv(_GRPC_MODEL, "1")
    host = "triton.svc.cluster.local"
    url = f"{scheme}://{host}:9001"

    with pytest.raises(UnsafeEndpointError) as exc:
        validate_outbound_url(url, endpoint_kind=ENDPOINT_KIND_MODEL)
    assert exc.value.reason == "internal_zone"
    assert exc.value.fixable_by_trust_host is True

    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", host)
    validate_outbound_url(url, endpoint_kind=ENDPOINT_KIND_MODEL)


def test_grpc_single_label_host_still_blocked(monkeypatch):
    """``grpc://triton:9001`` 這種 compose service name 仍要擋(可用信任清單修)。"""
    monkeypatch.setenv(_GRPC_MODEL, "1")
    with pytest.raises(UnsafeEndpointError) as exc:
        validate_outbound_url("grpc://triton:9001", endpoint_kind=ENDPOINT_KIND_MODEL)
    assert exc.value.reason == "single_label"
    assert exc.value.fixable_by_trust_host is True
