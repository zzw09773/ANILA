"""HTTP client: agent reads another user's facts via service token.

Talks to the CSP cross-tenant endpoint introduced in route-3
Phase 3:

    GET {base}/api/memory/users/{user_id}/facts
    Header: X-CSP-Service-Token: <agent service token>

The endpoint returns ``{"total": int, "facts": [FactResponse, ...]}``
which we map to a list of :class:`UserFactDTO` so callers see the
same DTO shape they'd get from a local :class:`MemoryAdapter`
implementation.

Failure modes are surfaced as a dedicated
:class:`UserFactReadError` rather than the underlying httpx /
HTTP-status exceptions so the agent runtime can degrade gracefully
("memory read failed; answering without long-term context") with a
single except clause.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

import httpx

from ..models import UserFactDTO


class UserFactReadError(Exception):
    """Raised when the cross-tenant facts read fails for any reason
    (network, auth, server, parse). Callers typically catch + log
    + degrade to "no facts" rather than propagate.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = None,
        response_snippet: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_snippet = response_snippet


class HttpUserFactReader:
    """Read-only client for the cross-tenant user-facts endpoint.

    Construct once per agent process; safe for concurrent use across
    user_ids. The httpx client is built per call (not pooled) because
    cross-tenant reads are infrequent enough that connection reuse
    isn't worth the lifecycle complexity here.

    Example::

        reader = HttpUserFactReader(
            base_url="http://csp:8000",
            service_token=os.environ["AGENT_SERVICE_TOKEN"],
        )
        facts = await reader.get_user_facts(user_id=42)

    The constructor doesn't validate the token; the first call
    surfaces a 401 as :class:`UserFactReadError` with status_code=401.
    """

    def __init__(
        self,
        *,
        base_url: str,
        service_token: str,
        timeout_seconds: float = 5.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._service_token = service_token
        self._timeout = timeout_seconds

    async def get_user_facts(self, user_id: int) -> list[UserFactDTO]:
        url = f"{self._base_url}/api/memory/users/{user_id}/facts"
        headers = {"X-CSP-Service-Token": self._service_token}
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.get(url, headers=headers)
        except httpx.RequestError as exc:
            raise UserFactReadError(
                f"network error reading user_id={user_id} facts: {exc}",
            ) from exc

        if resp.status_code != 200:
            raise UserFactReadError(
                f"non-200 status reading user_id={user_id} facts",
                status_code=resp.status_code,
                response_snippet=resp.text[:300],
            )

        try:
            payload = resp.json()
            raw_facts = payload.get("facts") or []
        except (ValueError, AttributeError) as exc:
            raise UserFactReadError(
                f"unparseable response reading user_id={user_id} facts",
                response_snippet=resp.text[:300],
            ) from exc

        return [_fact_dict_to_dto(f) for f in raw_facts if isinstance(f, dict)]


def _fact_dict_to_dto(d: dict[str, Any]) -> UserFactDTO:
    """Map the JSON shape served by CSP's FactResponse → UserFactDTO.

    Tolerant of missing optional fields; required fields raise
    KeyError which the caller treats as a malformed response.
    """
    return UserFactDTO(
        id=d.get("id"),
        user_id=int(d["user_id"]) if "user_id" in d else 0,
        key=d["key"],
        value=d["value"],
        confidence=float(d.get("confidence", 1.0)),
        source_conversation_id=d.get("source_conversation_id"),
        source_message_id=d.get("source_message_id"),
        created_at=_parse_dt(d.get("created_at")),
        updated_at=_parse_dt(d.get("updated_at")),
    )


def _parse_dt(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            # Pydantic serialises tz-aware datetimes as ISO 8601.
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None
