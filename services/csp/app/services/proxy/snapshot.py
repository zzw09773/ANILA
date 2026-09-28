"""Plain copy of a model row for the upstream call.

The request session is closed before the model wait. An ORM instance
passed into that wait would check a connection back out on the first
expired attribute and hold it until the response finished. Every
internal caller uses this helper so ``max_concurrent`` is not dropped
and the concurrency gate is not skipped.
"""

from __future__ import annotations

from types import SimpleNamespace


def snapshot_model(model):
    if model is None:
        return None
    if isinstance(model, SimpleNamespace):
        return SimpleNamespace(**vars(model))
    from sqlalchemy import inspect as sa_inspect

    mapper = sa_inspect(model).mapper
    return SimpleNamespace(
        **{attr.key: getattr(model, attr.key) for attr in mapper.column_attrs}
    )
