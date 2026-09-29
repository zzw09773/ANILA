"""模型與 Agent 離線要走和其他警報同一條寄信：進入 unhealthy 寄一次，重複不寄，解決後再進入才再寄。"""
from __future__ import annotations

from app.services.alert_notifier import set_notifier
from app.services.health_checker import (
    HEALTH_HEALTHY,
    HEALTH_UNHEALTHY,
    apply_agent_health_results,
    apply_model_health_results,
)
from tests.conftest import make_agent, make_model, make_user


class _Notifier:
    def __init__(self) -> None:
        self.sent = []

    def send(self, notification) -> None:
        self.sent.append(notification)


def test_model_offline_mails_on_transition_and_not_again_until_reopen(db):
    model = make_model(db, name="mail-model")
    secret_url = "http://mock-llm:8080/secret"
    notifier = _Notifier()
    previous = set_notifier(notifier)
    row = (
        model.id,
        secret_url,
        model.name,
        model.display_name,
        model.health_status,
        HEALTH_UNHEALTHY,
        None,
    )
    try:
        apply_model_health_results(db, [row])
        db.commit()
        apply_model_health_results(db, [row])
        db.commit()
        assert len(notifier.sent) == 1
        note = notifier.sent[0]
        assert note.fingerprint == f"health:model:{model.id}"
        assert note.severity == "high"
        assert secret_url not in note.message
        assert secret_url not in note.title

        healthy = row[:-2] + (HEALTH_HEALTHY, None)
        apply_model_health_results(db, [healthy])
        db.commit()
        apply_model_health_results(db, [row])
        db.commit()
        assert len(notifier.sent) == 2
    finally:
        set_notifier(previous)


def test_agent_offline_mails_on_transition_and_not_again_until_reopen(db):
    owner = make_user(db, username="agent-mail-owner", role="user")
    agent = make_agent(db, owner, name="mail-agent", approval_status="approved")
    secret_url = "http://agent:9100/secret"
    notifier = _Notifier()
    previous = set_notifier(notifier)
    row = (agent.id, secret_url, agent.name, agent.health_status, HEALTH_UNHEALTHY)
    try:
        apply_agent_health_results(db, [row])
        db.commit()
        apply_agent_health_results(db, [row])
        db.commit()
        assert len(notifier.sent) == 1
        note = notifier.sent[0]
        assert note.fingerprint == f"health:agent:{agent.id}"
        assert secret_url not in note.message
        assert secret_url not in note.title

        healthy = row[:-1] + (HEALTH_HEALTHY,)
        apply_agent_health_results(db, [healthy])
        db.commit()
        apply_agent_health_results(db, [row])
        db.commit()
        assert len(notifier.sent) == 2
    finally:
        set_notifier(previous)
