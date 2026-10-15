"""Failed completion persistence uses the existing conversation owner and writer gates."""
import pytest

from app.services.completion_metadata import completion_metadata
from tests.test_quick_builder import http as _http, people as _people, login

http = _http
people = _people


@pytest.mark.parametrize("reason", ["length", "content_filter"])
@pytest.mark.parametrize("denial", ["wrong_writer", "outsider", "anonymous"])
def test_failed_completion_save_reload_preserves_owner_and_writer(http, people, reason, denial):
    owner = login(http, "creator")
    other = login(http, "outsider")
    created = http.post("/api/conversations", json={"title": "合成HTTP502失敗保存"}, headers=owner)
    assert created.status_code == 201
    cid = created.json()["id"]
    question = http.post(f"/api/conversations/{cid}/messages", json={"role": "user", "content": "合成問題"}, headers=owner)
    assert question.status_code == 201
    writer = "synthetic-http502-writer"
    reserved = http.post(f"/api/conversations/{cid}/messages/{question.json()['id']}/reserve-reply", json={"stream_writer": writer}, headers=owner)
    assert reserved.status_code == 201
    mid = reserved.json()["id"]
    path = f"/api/conversations/{cid}/messages/{mid}"
    completion = completion_metadata(finish_reason=reason, terminal_source="response", usage={"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7})
    payload = {"content": "", "metadata": {"completion": completion, "anila_stream": {"state": "failed"}}, "stream_writer": writer}
    denied_headers = owner if denial == "wrong_writer" else other if denial == "outsider" else {}
    denied = http.put(path, json={**payload, "stream_writer": "wrong-synthetic-writer"} if denial == "wrong_writer" else payload, headers=denied_headers)
    assert denied.status_code == {"wrong_writer": 409, "outsider": 404, "anonymous": 401}[denial]
    untouched = http.get(f"/api/conversations/{cid}?view=all", headers=owner)
    row = next(item for item in untouched.json()["messages"] if item["id"] == mid)
    assert row["metadata"]["anila_stream"]["state"] == "reserved"
    assert "completion" not in row["metadata"]
    saved = http.put(path, json=payload, headers=owner)
    assert saved.status_code == 200
    assert saved.json()["metadata"]["anila_stream"] == {"state": "failed"}
    assert saved.json()["metadata"]["completion"] == completion
    reload = http.get(f"/api/conversations/{cid}?view=all", headers=owner)
    row = next(item for item in reload.json()["messages"] if item["id"] == mid)
    assert row["content"] == ""
    assert row["metadata"]["completion"]["usage"]["total_tokens"] == 7
    assert row["metadata"]["completion"]["answer_complete"] is False
    assert http.get(f"/api/conversations/{cid}?view=all", headers=other).status_code == 404
    assert http.put(path, json={"metadata": {"completion": completion, "anila_stream": {"state": "complete"}}}, headers=owner).status_code == 409
