"""Shared path for persisting + publishing a token-version bump.

Every site that increments ``users.token_version`` to invalidate JWTs
must call ``commit_token_revocation`` after the bump so downstream
consumers (anila-studio / asr-gateway) learn via the deny-list, not
only csp's direct ``tv == users.token_version`` check.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.platform_setting import get_setting
from app.models.token_revocation import TokenRevocation
from app.models.user import User
from app.services import token_revocation_publisher


def commit_token_revocation(db: Session, user: User) -> int:
    """Persist and publish a token-version revocation after bumping user.

    ⚠ Contract with every consumer (anila-studio / asr-gateway revocation
    caches): ``revoked_at_version`` is the **post-bump** ``token_version``,
    i.e. the lowest version that is still valid — NOT the highest version
    that is dead. It is exactly the ``tv`` claim that ``create_tokens``
    will stamp into the next token this user is issued, so consumers must
    reject ``tv < revoked_at_version`` and accept ``tv ==
    revoked_at_version``. Do not subtract one here: that would make every
    row already in ``token_revocations`` mean something different, and the
    consumers would silently start honouring the last generation of
    pre-revocation tokens. Pinned by
    ``services/csp/tests/test_token_revoke_publish.py`` and
    ``services/csp/tests/test_deactivate_revokes_session.py``.
    """
    lowest_valid_version = int(user.token_version or 0)
    # ⚠ **在 commit 之前解析**（Task 3 的 pool 教訓）：``db.commit()`` 之後那條連線就
    # 還回池子了，這時再查設定會是另一次借還。逾時值以必填關鍵字往下傳 ——
    # 發布端沒有第二份預設值可以與設定頁漂開（畫面說 2.0、連線用 999 的那種分歧）。
    redis_timeout = float(get_setting(db, "queue.token_revocation_redis_timeout"))
    db.add(
        TokenRevocation(user_id=user.id, revoked_at_version=lowest_valid_version)
    )
    db.commit()
    # Call through the module so test monkeypatches on
    # ``token_revocation_publisher.publish_revocation_sync`` still apply.
    token_revocation_publisher.publish_revocation_sync(
        user_id=user.id,
        revoked_at_version=lowest_valid_version,
        timeout=redis_timeout,
    )
    return lowest_valid_version
