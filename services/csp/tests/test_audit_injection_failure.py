"""A failed prompt-injection audit is not discarded."""

import pytest

from anila_core.security.external_content import Finding
from app.api import proxy as proxy_api


def test_audit_commit_failure_raises(monkeypatch):
    class DB:
        def commit(self):
            raise RuntimeError("db down")

        def rollback(self):
            self.rolled = True

    monkeypatch.setattr(proxy_api, "log_audit_event", lambda *args, **kwargs: None)
    db = DB()
    with pytest.raises(RuntimeError, match="db down"):
        proxy_api._audit_injection(db, None, [Finding("input", "doc", "rule")])
    assert db.rolled is True
