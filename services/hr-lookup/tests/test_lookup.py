"""用塞進 sys.modules 的假 oracledb 測固定查詢。沒有真的 Oracle。"""
from __future__ import annotations

import json
import sys
import threading
from http.client import HTTPConnection
from types import ModuleType

import pytest

import server


PASSWORD = "super-secret-oracle-password"
EMPLOYEE = "1147259"
TABLE = "csiih.vihbuy"


class _Cursor:
    def __init__(self, conn):
        self.conn = conn
        self.closed = False

    def execute(self, sql, binds):
        self.conn.sql = sql
        self.conn.binds = binds
        self.conn.module.sqls.append(sql)
        select = sql.split("FROM", 1)[0]
        if self.conn.module.fail_unquoted and '"' not in select:
            raise RuntimeError('ORA-00904: "OVC_PNO": invalid identifier')
        if self.conn.execute_error is not None:
            raise self.conn.execute_error

    def fetchmany(self, size):
        self.conn.fetch_size = size
        return list(self.conn.rows)

    def close(self):
        self.closed = True


class _Connection:
    def __init__(self, module):
        self.module = module
        self.sql = None
        self.binds = None
        self.fetch_size = None
        self.rows = module.rows
        self.execute_error = module.execute_error
        self.closed = False
        self.call_timeout = None

    def cursor(self):
        return _Cursor(self)

    def close(self):
        self.closed = True
        if self.module.close_error is not None:
            raise self.module.close_error


class _Oracle(ModuleType):
    def __init__(self):
        super().__init__("oracledb")
        self.connect_kwargs = None
        self.connect_error = None
        self.execute_error = None
        self.close_error = None
        self.rows = []
        self.connections = []
        self.sqls = []
        self.fail_unquoted = False
        self.init_called = False

    def connect(self, **kwargs):
        self.connect_kwargs = kwargs
        if self.connect_error is not None:
            raise self.connect_error
        conn = _Connection(self)
        self.connections.append(conn)
        return conn

    def init_oracle_client(self, *args, **kwargs):
        self.init_called = True


@pytest.fixture
def oracle(monkeypatch):
    module = _Oracle()
    monkeypatch.setitem(sys.modules, "oracledb", module)
    return module


def _payload(**overrides):
    body = {
        "host": "10.1.2.3",
        "port": 1521,
        "service_name": "HRPDB",
        "user": "hr_reader",
        "password": PASSWORD,
        "table_name": TABLE,
        "employee_no": EMPLOYEE,
    }
    body.update(overrides)
    return body


def test_sql_uses_bind_variable_timeouts_and_validated_table(oracle):
    oracle.rows = [(EMPLOYEE, "王小明", "資訊通信研究所", "人工智慧組", "a@b.example", "組長")]
    result = server.query_staff(_payload())
    assert result["ok"] is True
    assert result["rows"] == [{
        "ovc_PNO": EMPLOYEE,
        "ovc_NAME": "王小明",
        "ovc_DEPT1_NAME": "資訊通信研究所",
        "ovc_DEPT2_NAME": "人工智慧組",
        "ovc_EMAIL": "a@b.example",
        "ovc_DUTY_DS": "組長",
    }]
    kwargs = oracle.connect_kwargs
    assert kwargs["tcp_connect_timeout"] == 5
    assert kwargs["password"] == PASSWORD
    assert "init_oracle_client" not in kwargs
    assert oracle.init_called is False
    conn = oracle.connections[0]
    assert conn.call_timeout == 5000
    assert conn.closed is True
    assert conn.fetch_size == server.ROW_CAP + 1
    assert ":pno" in conn.sql
    assert TABLE in conn.sql
    assert EMPLOYEE not in conn.sql
    assert PASSWORD not in conn.sql
    assert conn.binds == {"pno": EMPLOYEE}
    for column in server.COLUMNS:
        assert column in conn.sql


def test_connection_closes_when_query_raises(oracle):
    oracle.execute_error = RuntimeError(f"ORA-00942 missing {PASSWORD}")
    result = server.query_staff(_payload())
    assert result["ok"] is False
    assert result["error_type"] == "query_failed"
    assert result["oracle_code"] == "ORA-00942"
    assert PASSWORD not in result["message"]
    assert PASSWORD not in json.dumps(result)
    assert oracle.connections[0].closed is True


def test_connect_error_includes_type_and_code_without_password(oracle):
    oracle.connect_error = TimeoutError(f"ORA-12541 listener {PASSWORD}")
    result = server.query_staff(_payload())
    assert result["error_type"] == "connect_failed"
    assert result["oracle_code"] == "ORA-12541"
    assert "connect_failed" in result["message"]
    assert "ORA-12541" in result["message"]
    assert PASSWORD not in json.dumps(result)
    assert oracle.connections == []


def test_driver_missing(monkeypatch):
    monkeypatch.setitem(sys.modules, "oracledb", None)
    real_import = __import__

    def _import(name, *args, **kwargs):
        if name == "oracledb":
            raise ImportError("no module named oracledb")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", _import)
    result = server.query_staff(_payload())
    assert result["error_type"] == "driver_missing"
    assert "Oracle 驅動" in result["message"]
    assert PASSWORD not in json.dumps(result)


@pytest.mark.parametrize(
    "table_name",
    ["", "1bad", "hr.table.extra", "hr;drop", "hr table", "hr'--", "vihbuy ", ".vihbuy"],
)
def test_table_name_rejected_before_connect(oracle, table_name):
    result = server.query_staff(_payload(table_name=table_name))
    assert result["error_type"] == "invalid_table"
    assert oracle.connect_kwargs is None


def test_too_many_rows(oracle):
    oracle.rows = [(EMPLOYEE, "同", "所", "組", None, "組長")] * (server.ROW_CAP + 1)
    result = server.query_staff(_payload())
    assert result["error_type"] == "too_many_rows"
    assert "rows" not in result


def test_pno_mismatch_is_not_returned(oracle):
    oracle.rows = [("999999", "別人", "所", "組", None, None)]
    result = server.query_staff(_payload())
    assert result["error_type"] == "pno_mismatch"


def test_caller_sql_is_ignored(oracle):
    oracle.rows = [(EMPLOYEE, "王", "所", "組", None, None)]
    result = server.query_staff(_payload(sql="SELECT * FROM secret", columns=["a"]))
    assert result["ok"] is True
    assert "SELECT *" not in oracle.connections[0].sql
    assert oracle.connections[0].sql.startswith("SELECT ovc_PNO")


def test_health_does_not_touch_oracle_and_logs_skip_the_body(oracle, capsys):
    httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        health = HTTPConnection("127.0.0.1", port, timeout=3)
        health.request("GET", "/health")
        response = health.getresponse()
        assert response.status == 200
        assert json.loads(response.read().decode()) == {"status": "ok"}
        assert oracle.connect_kwargs is None

        body = json.dumps(_payload()).encode()
        lookup = HTTPConnection("127.0.0.1", port, timeout=3)
        lookup.request("POST", "/lookup", body=body, headers={"Content-Length": str(len(body))})
        looked = lookup.getresponse()
        looked.read()
        captured = capsys.readouterr()
        assert PASSWORD not in captured.err
        assert PASSWORD not in captured.out
        assert "POST /lookup" in captured.err
    finally:
        httpd.shutdown()
        thread.join(timeout=3)


def test_ora_00904_retries_once_with_quoted_names_and_reads_by_position(oracle):
    oracle.fail_unquoted = True
    oracle.rows = [(EMPLOYEE, "王小明", "資訊通信研究所", "人工智慧組", None, "組長")]
    result = server.query_staff(_payload())
    assert result["ok"] is True
    assert result["rows"][0]["ovc_NAME"] == "王小明"
    assert result["rows"][0]["ovc_DUTY_DS"] == "組長"
    assert len(oracle.sqls) == 2
    assert oracle.sqls[0].startswith("SELECT ovc_PNO, ovc_NAME,")
    quoted = oracle.sqls[1]
    for name in (
        "ovc_PNO",
        "ovc_NAME",
        "ovc_DEPT1_NAME",
        "ovc_DEPT2_NAME",
        "ovc_EMAIL",
        "ovc_DUTY_DS",
    ):
        assert f'"{name}"' in quoted
    assert quoted.startswith('SELECT "ovc_PNO"')
    assert "WHERE \"ovc_PNO\" = :pno" in quoted

    oracle.sqls.clear()
    oracle.fail_unquoted = False
    oracle.execute_error = RuntimeError("ORA-00904: still missing")
    failed = server.query_staff(_payload())
    assert failed["ok"] is False
    assert failed["error_type"] == "query_failed"
    assert failed["oracle_code"] == "ORA-00904"
    assert len(oracle.sqls) == 2
    assert oracle.sqls[1].startswith('SELECT "ovc_PNO"')


def test_lookup_over_the_slot_limit_is_503_and_does_not_connect(oracle):
    held = threading.BoundedSemaphore(1)
    assert held.acquire(blocking=False)
    previous = server._slots
    server._slots = held
    httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        body = json.dumps(_payload()).encode()
        lookup = HTTPConnection("127.0.0.1", port, timeout=3)
        lookup.request("POST", "/lookup", body=body, headers={"Content-Length": str(len(body))})
        response = lookup.getresponse()
        payload = json.loads(response.read().decode())
        assert response.status == 503
        assert payload["error_type"] == "busy"
        assert oracle.connect_kwargs is None
        assert oracle.connections == []
    finally:
        held.release()
        server._slots = previous
        httpd.shutdown()
        thread.join(timeout=3)


def test_timeout_error_type(oracle):
    oracle.connect_error = TimeoutError("timed out waiting")
    result = server.query_staff(_payload())
    assert result["error_type"] == "timeout"
    assert "timeout" in result["message"]
