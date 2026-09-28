"""測試用：把一個嵌入模型標成平台嵌入角色。"""
from __future__ import annotations

from app.models.model_registry import ModelRegistry


def designate_embedding(db, name: str = "nv-embed") -> ModelRegistry:
    row = db.query(ModelRegistry).filter(ModelRegistry.name == name).one_or_none()
    if row is None:
        row = ModelRegistry(
            name=name,
            display_name=name,
            model_type="embedding",
            endpoint_url="http://embed.test/v1",
            is_active=True,
            is_platform_embedding=True,
            embedding_native_dim=4096,
        )
        db.add(row)
    else:
        row.model_type = "embedding"
        row.is_active = True
        row.is_platform_embedding = True
    db.commit()
    db.refresh(row)
    return row
