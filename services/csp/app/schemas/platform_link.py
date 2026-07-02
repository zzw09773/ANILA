from datetime import datetime
from pydantic import BaseModel, Field, field_validator

# Roles defined on User.role today: 'owner' / 'admin' / 'user' / 'developer'.
# Tier order (high → low): owner > admin > developer ≈ user. ``owner`` is
# the platform-operator role added in 0033 — gating role-altering ops,
# auth provider edits, and hard-purge endpoints.
# An empty list means the role gate is open (any role passes); a non-empty
# list means the user's role must be in the list. Validated at the API
# boundary so a typo ('Admin', 'dev') fails fast rather than silently
# locking everyone out.
_ALLOWED_ROLES = {"owner", "admin", "user", "developer"}


def _validate_required_roles(value: list[str] | None) -> list[str] | None:
    if value is None:
        return None
    bad = [r for r in value if r not in _ALLOWED_ROLES]
    if bad:
        raise ValueError(
            f"required_roles contains unknown role(s): {bad}. "
            f"Allowed: {sorted(_ALLOWED_ROLES)}"
        )
    return value


class PlatformLinkCreate(BaseModel):
    name: str
    url: str
    icon: str | None = None
    description: str | None = None
    sort_order: int = 0
    is_public: bool = False
    required_roles: list[str] = Field(default_factory=list)

    @field_validator("required_roles")
    @classmethod
    def _check_required_roles(cls, v: list[str]) -> list[str]:
        return _validate_required_roles(v) or []


class PlatformLinkUpdate(BaseModel):
    name: str | None = None
    url: str | None = None
    icon: str | None = None
    description: str | None = None
    sort_order: int | None = None
    is_active: bool | None = None
    is_public: bool | None = None
    required_roles: list[str] | None = None

    @field_validator("required_roles")
    @classmethod
    def _check_required_roles(cls, v: list[str] | None) -> list[str] | None:
        return _validate_required_roles(v)


class PlatformLinkResponse(BaseModel):
    id: int
    name: str
    url: str
    icon: str | None
    description: str | None
    sort_order: int
    is_active: bool
    is_public: bool
    required_roles: list[str]
    created_at: datetime

    model_config = {"from_attributes": True}
