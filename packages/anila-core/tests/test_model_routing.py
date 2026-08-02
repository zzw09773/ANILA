"""任務→模型路由契約的守護測試。

不變式：
1. ``TASK_CLASS`` 正好涵蓋文件記載的五鍵（rag_qa／chat／chips／json_gen／title）。
2. 若 ``prompts/sampling.py`` 已合併，鍵集合須與 ``TASK_SAMPLING`` 一致；
   尚未合併則條件測試 skip（合併後自動生效）。
3. env 未設 → ``resolve_model`` 回傳呼叫端 ``default``（零行為變化）。
4. 依 class 讀 ``ANILA_MODEL_ANALYSIS``／``ANILA_MODEL_FAST``；空字串視同未設。
5. 未知 task 拋 KeyError，訊息含已知鍵清單。
"""

from __future__ import annotations

import pytest

from anila_core.prompts.model_routing import TASK_CLASS, resolve_model

_EXPECTED_KEYS = frozenset({"rag_qa", "chat", "chips", "json_gen", "title"})
_ANALYSIS_TASKS = ("rag_qa", "chat")
_FAST_TASKS = ("chips", "json_gen", "title")


def test_task_class_keys_match_documented_set():
    assert frozenset(TASK_CLASS) == _EXPECTED_KEYS
    for task in _ANALYSIS_TASKS:
        assert TASK_CLASS[task] == "analysis"
    for task in _FAST_TASKS:
        assert TASK_CLASS[task] == "fast"


def test_task_class_keys_align_with_sampling_table_when_present():
    try:
        from anila_core.prompts.sampling import TASK_SAMPLING
    except ImportError:
        pytest.skip("sampling table 尚未合併——合併後本測試自動生效")
    assert set(TASK_CLASS) == set(TASK_SAMPLING)


def test_resolve_model_returns_default_when_env_unset(monkeypatch):
    monkeypatch.delenv("ANILA_MODEL_ANALYSIS", raising=False)
    monkeypatch.delenv("ANILA_MODEL_FAST", raising=False)
    assert resolve_model("rag_qa", default="gemma4") == "gemma4"
    assert resolve_model("chips", default="gemma4") == "gemma4"


def test_resolve_model_returns_default_when_env_blank(monkeypatch):
    monkeypatch.setenv("ANILA_MODEL_ANALYSIS", "   ")
    monkeypatch.setenv("ANILA_MODEL_FAST", "")
    assert resolve_model("chat", default="keep-me") == "keep-me"
    assert resolve_model("title", default="keep-me") == "keep-me"


def test_resolve_model_uses_analysis_env(monkeypatch):
    monkeypatch.setenv("ANILA_MODEL_ANALYSIS", "gemma26")
    monkeypatch.setenv("ANILA_MODEL_FAST", "gemma26-nothink")
    assert resolve_model("rag_qa", default="ignored") == "gemma26"
    assert resolve_model("chat", default="ignored") == "gemma26"


def test_resolve_model_uses_fast_env(monkeypatch):
    monkeypatch.setenv("ANILA_MODEL_ANALYSIS", "gemma26")
    monkeypatch.setenv("ANILA_MODEL_FAST", "gemma26-nothink")
    assert resolve_model("chips", default="ignored") == "gemma26-nothink"
    assert resolve_model("json_gen", default="ignored") == "gemma26-nothink"
    assert resolve_model("title", default="ignored") == "gemma26-nothink"


def test_resolve_model_strips_padded_env(monkeypatch):
    monkeypatch.setenv("ANILA_MODEL_ANALYSIS", "  gemma26\t")
    monkeypatch.setenv("ANILA_MODEL_FAST", "  gemma26-nothink\t")
    assert resolve_model("rag_qa", default="ignored") == "gemma26"
    assert resolve_model("chips", default="ignored") == "gemma26-nothink"


def test_unknown_task_raises_keyerror_with_known_keys():
    with pytest.raises(KeyError) as excinfo:
        resolve_model("not-a-task", default="x")
    msg = str(excinfo.value)
    assert "not-a-task" in msg
    for key in _EXPECTED_KEYS:
        assert key in msg
