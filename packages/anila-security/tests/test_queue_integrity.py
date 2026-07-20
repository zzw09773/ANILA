from __future__ import annotations

import pytest

from anila_security.queue_integrity import create_queue_proof, verify_queue_proof


KEY = "gate3-queue-integrity-test-key-0001"
PAYLOAD = {"document_id": 17, "attempt_number": 2}


def test_queue_proof_round_trip() -> None:
    proof = create_queue_proof(KEY, task_name="ingest_document", payload=PAYLOAD)

    verify_queue_proof(
        KEY,
        task_name="ingest_document",
        payload=PAYLOAD,
        proof=proof,
    )


@pytest.mark.parametrize(
    ("task_name", "payload", "proof"),
    [
        ("reresolve_collection_relations", PAYLOAD, None),
        ("reresolve_collection_relations", PAYLOAD, "0" * 64),
        ("other_task", PAYLOAD, None),
        ("ingest_document", {"document_id": 18, "attempt_number": 2}, None),
    ],
)
def test_queue_proof_rejects_missing_or_tampered_authority(
    task_name: str,
    payload: dict[str, int],
    proof: str | None,
) -> None:
    valid = create_queue_proof(
        KEY, task_name="ingest_document", payload=PAYLOAD
    )

    with pytest.raises(PermissionError, match="integrity proof"):
        verify_queue_proof(
            KEY,
            task_name=task_name,
            payload=payload,
            proof=proof if proof is not None else valid,
        )


def test_queue_proof_rejects_short_key() -> None:
    with pytest.raises(ValueError, match="at least 32"):
        create_queue_proof("too-short", task_name="ingest_document", payload=PAYLOAD)
