"""Connection cap is global. real_ip stays commented until a proxy is listed."""

from pathlib import Path

_CONF = Path(__file__).resolve().parents[3] / "infra" / "nginx" / "anila.conf"


def test_one_intranet_ip_is_not_a_small_connection_bucket():
    text = _CONF.read_text(encoding="utf-8")
    assert "limit_conn global_conn 16384;" in text
    assert text.count("limit_conn global_conn 16384;") == 2
    assert "limit_conn sse_conn" not in text
    assert "limit_conn conn_limit" not in text
    assert "limit_conn_zone $binary_remote_addr" not in text
    active = [
        line.strip()
        for line in text.splitlines()
        if line.strip().startswith(("set_real_ip_from", "real_ip_header", "real_ip_recursive"))
    ]
    assert active == []
    assert "# set_real_ip_from" in text
