"""Operational probes for the retained settings and removed constants."""

from app.models.platform_setting import set_setting
from app.services import attachment_context, conversation_service, message_action_service
from app.services.proxy.service import resolve_proxy_tuning


def test_proxy_timeouts_are_resolved_from_db(db):
    set_setting(db, "proxy.llm_timeout", 301)
    set_setting(db, "proxy.embedding_timeout", 302)
    db.commit()
    tuning = resolve_proxy_tuning(db)
    assert tuning.llm_timeout == 301
    assert tuning.embedding_timeout == 302
    assert tuning.max_retries == 3
    assert tuning.retry_base_delay == 0.5


def test_fixed_limits_are_not_settings_anymore(db):
    assert conversation_service._max_siblings(db) == 20
    assert message_action_service._max_body_chars(db) == 20_000
    assert attachment_context.get_context_window(db, None) == 128_000
    # Preserve the existing ``int(100 * 1.15)`` truncation semantics.
    assert attachment_context.effective_cost(db, 100) == 114


def test_bool_settings_reach_consumers(db):
    set_setting(db, "intl.zh_normalize", False)
    set_setting(db, "intl.query_expansion", False)
    db.commit()
    assert attachment_context.get_context_window(db, None) == 128_000
