"""Admin Valves get / put with AES-256-GCM at rest.

GET strips secret-tagged fields to ``{has_value: True}``; only admin can
PUT (per spec §7.1 RBAC). PUT is upsert. The crypto key is loaded by
``valves_crypto`` from ``ANILA_FUNCTIONS_VALVES_KEY`` so we never see
plaintext key material in this module.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.auth import get_current_user
from app.database import get_db
from app.models import ActionFunctionValves, User
from app.schemas.action_function import ValvesValues, ValvesValuesRead
from app.services.action_function import crud as fn_crud
from app.services.action_function.valves_crypto import (
    decrypt_valves,
    encrypt_valves,
)


router = APIRouter(prefix="/api/functions", tags=["action-functions"])


def _author_or_admin(user: User, fn) -> bool:
    return user.role == "admin" or fn.author_user_id == user.id


def _strip_secrets_for_read(values: dict, schema: dict) -> dict:
    """Replace fields tagged secret with ``{has_value: bool}``.

    Schema flag detection accepts either pydantic's
    ``json_schema_extra.secret`` or a top-level ``x-secret``. Plain
    fields pass through unmodified.
    """
    out: dict = {}
    props = schema.get("properties") or {}
    for k, v in values.items():
        meta = props.get(k, {})
        is_secret = (
            (meta.get("json_schema_extra") or {}).get("secret")
            or meta.get("x-secret")
        )
        if is_secret:
            out[k] = {"has_value": bool(v)}
        else:
            out[k] = v
    return out


@router.get(
    "/{slug}/valves", response_model=ValvesValuesRead
)
def get_valves(
    slug: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    fn = fn_crud.get_function_by_slug(db, slug)
    if fn is None:
        raise HTTPException(status_code=404)
    if not _author_or_admin(user, fn):
        raise HTTPException(status_code=403)
    row = (
        db.query(ActionFunctionValves)
        .filter_by(function_id=fn.id)
        .first()
    )
    if row is None:
        return ValvesValuesRead(fields={})
    plaintext = decrypt_valves(row.values_encrypted, row.nonce, row.key_version)
    latest = fn_crud.get_latest_version(db, fn.id)
    schema = latest.valves_schema_json if latest else {}
    return ValvesValuesRead(
        fields=_strip_secrets_for_read(plaintext, schema)
    )


@router.put("/{slug}/valves", status_code=204)
def put_valves(
    slug: str,
    payload: ValvesValues,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if user.role != "admin":
        raise HTTPException(
            status_code=403, detail="admin role required to set Valves"
        )
    fn = fn_crud.get_function_by_slug(db, slug)
    if fn is None:
        raise HTTPException(status_code=404)
    ciphertext, nonce, key_version = encrypt_valves(payload.values)
    row = (
        db.query(ActionFunctionValves)
        .filter_by(function_id=fn.id)
        .first()
    )
    if row is None:
        row = ActionFunctionValves(
            function_id=fn.id,
            values_encrypted=ciphertext,
            nonce=nonce,
            key_version=key_version,
            updated_by=user.id,
        )
        db.add(row)
    else:
        row.values_encrypted = ciphertext
        row.nonce = nonce
        row.key_version = key_version
        row.updated_by = user.id
    db.commit()
