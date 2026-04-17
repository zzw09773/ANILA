from sqlalchemy.orm import Session
from app.models.model_registry import ModelRegistry


def get_model_by_name(db: Session, model_name: str) -> ModelRegistry | None:
    return db.query(ModelRegistry).filter(ModelRegistry.name == model_name).first()


def get_active_models(db: Session) -> list[ModelRegistry]:
    return (
        db.query(ModelRegistry)
        .filter(ModelRegistry.is_active == True)
        .order_by(ModelRegistry.model_type, ModelRegistry.name)
        .all()
    )
