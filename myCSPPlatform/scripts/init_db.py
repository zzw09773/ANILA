"""Initialize database with admin user and sample platform links."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from app.database import engine, SessionLocal, Base
from app.models.user import User
from app.models.platform_link import PlatformLink
from app.utils.security import hash_password
from app.config import settings


def init_db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()

    try:
        # Create admin user if not exists
        admin = db.query(User).filter(User.username == settings.ADMIN_USERNAME).first()
        if not admin:
            admin = User(
                username=settings.ADMIN_USERNAME,
                hashed_password=hash_password(settings.ADMIN_PASSWORD),
                role="admin",
                is_active=True,
            )
            db.add(admin)
            print(f"已建立管理員帳號: {settings.ADMIN_USERNAME}")
        else:
            print("管理員帳號已存在")

        # Create sample platform links
        sample_links = [
            {
                "name": "n8n 工作流程",
                "url": "http://localhost:5678",
                "icon": "workflow",
                "description": "自動化工作流程平台",
                "sort_order": 1,
            },
            {
                "name": "GitLab",
                "url": "http://localhost:8929",
                "icon": "git",
                "description": "程式碼版本控制",
                "sort_order": 2,
            },
            {
                "name": "ANILA LM",
                "url": "http://localhost:3000",
                "icon": "notebook",
                "description": "AI 筆記本",
                "sort_order": 3,
            },
        ]

        existing_count = db.query(PlatformLink).count()
        if existing_count == 0:
            for link_data in sample_links:
                link = PlatformLink(**link_data)
                db.add(link)
            print(f"已建立 {len(sample_links)} 個平台連結範例")
        else:
            print(f"平台連結已存在 ({existing_count} 筆)")

        db.commit()
        print("資料庫初始化完成！")

    except Exception as e:
        db.rollback()
        print(f"初始化失敗: {e}")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    init_db()
