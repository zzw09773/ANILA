"""The callback envelope never forwards arbitrary provider metadata."""
from __future__ import annotations

import json

import pytest

from app.services.completion_metadata import (
    CompletionMetadataError, completion_metadata, normalize_completion,
)


@pytest.mark.parametrize("reason,code", [
    (None, None), ("stop", None), ("length", "MODEL_OUTPUT_LIMIT"),
    ("content_filter", "MODEL_CONTENT_FILTER"),
    ("tool_calls", "MODEL_ACTION_REQUIRED"),
    ("function_call", "MODEL_ACTION_REQUIRED"),
])
def test_canonical_completion_is_json_and_reason_controls_completeness(reason, code):
    meta = completion_metadata(finish_reason=reason)
    assert meta["schema_version"] == 1
    assert meta["answer_complete"] is (code is None)
    assert meta["usage_complete"] is True and meta["can_continue"] is False
    assert meta.get("type") == code
    if code:
        assert meta["message"].endswith(f"（{code}）")
    assert json.loads(json.dumps(meta)) == meta


def test_legacy_text_and_done_only_do_not_invent_an_observed_stop():
    legacy = normalize_completion("原有文字")
    assert legacy == {"text": "原有文字", "completion": completion_metadata()}
    done = completion_metadata(terminal_source="done")
    assert done["finish_reason"] is None and done["answer_complete"] is True


def test_usage_is_projected_and_unknown_or_unavailable_values_are_not_forwarded():
    meta = completion_metadata(usage={
        "prompt_tokens": 8, "completion_tokens": 3, "total_tokens": 11,
        "reasoning_tokens": None, "provider_message": "DO_NOT_FORWARD",
        "raw": {"credential": "DO_NOT_FORWARD"},
    })
    assert meta["usage"] == {"prompt_tokens": 8, "completion_tokens": 3, "total_tokens": 11}
    assert "DO_NOT_FORWARD" not in json.dumps(meta)
    clean = completion_metadata(usage={"reasoning_tokens": 2.0})["usage"]
    assert clean == {"reasoning_tokens": 2} and type(clean["reasoning_tokens"]) is int


@pytest.mark.parametrize("reason", ["length", "content_filter", "tool_calls", "function_call"])
def test_normalizer_reconstructs_fixed_messages_and_copies_usage(reason):
    source = {"text": "部分", "completion": completion_metadata(finish_reason=reason, usage={"total_tokens": 4})}
    source["completion"].update(type="PROVIDER_TYPE", message="DO_NOT_FORWARD")
    clean = normalize_completion(source)
    assert clean["completion"] == completion_metadata(finish_reason=reason, usage={"total_tokens": 4})
    source["completion"]["usage"]["total_tokens"] = 999
    assert clean["completion"]["usage"]["total_tokens"] == 4
    assert "DO_NOT_FORWARD" not in json.dumps(clean)


@pytest.mark.parametrize("kwargs", [
    {"finish_reason": "unknown"}, {"finish_reason": " STOP "},
    {"finish_reason": True}, {"finish_reason": []},
    {"terminal_source": "provider"}, {"terminal_source": "finish_reason"},
    {"terminal_source": "done", "finish_reason": "stop"},
    {"usage_complete": 1}, {"can_continue": "yes"}, {"usage": []},
    *[{"usage": {"total_tokens": n}} for n in [True, -1, "2", 1.5, float("nan"), float("inf"), 10**1000]],
])
def test_invalid_metadata_never_echoes_provider_values(kwargs):
    with pytest.raises(CompletionMetadataError) as error:
        completion_metadata(**kwargs)
    assert str(error.value) == "模型完成資訊格式不正確（MODEL_COMPLETION_METADATA）"


@pytest.mark.parametrize("mutation", [
    {"schema_version": 2}, {"schema_version": True},
    {"finish_reason": "provider"}, {"answer_complete": False},
    {"answer_complete": 1}, {"usage": None}, {"user_id": 999},
])
def test_normalizer_rejects_schema_mismatch_and_completion_flag_forgery(mutation):
    value = {"text": "內容", "completion": {**completion_metadata(), **mutation}}
    with pytest.raises(CompletionMetadataError):
        normalize_completion(value)


@pytest.mark.parametrize("value", [None, 1, [], {"text": "x"}, {"text": 1, "completion": {}}, {"text": "x", "completion": {}, "provider": "secret"}])
def test_only_legacy_strings_or_exact_envelopes_are_allowed(value):
    with pytest.raises(CompletionMetadataError):
        normalize_completion(value)


def test_length_cannot_be_normalized_as_a_complete_answer():
    value = {"text": "部分", "completion": completion_metadata(finish_reason="length")}
    value["completion"]["answer_complete"] = True
    with pytest.raises(CompletionMetadataError):
        normalize_completion(value)
