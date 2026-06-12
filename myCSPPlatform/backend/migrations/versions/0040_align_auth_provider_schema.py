"""Align ``auth_providers`` / ``external_identities`` schema with the live models.

R2 空機演練 (2026-06-11) 發現:這兩張表的 model 早已從「config jsonb」設計
重構成顯式 OIDC 欄位,但重構當時沒寫 migration — dev 環境一直靠 main.py 的
create_all fallback 用新 shape 建表,把洞蓋住;乾淨 alembic 鏈建出來的是
0012/0013 時代的舊 shape,runtime 第一次查 ``default_department_id`` 就 500
(GET /api/auth/providers,LoginView 載入必打)。

寫法全部走 IF NOT EXISTS / DO-block guard,因此在兩種來源的 DB 上都安全:
- 舊 shape (乾淨 migration 鏈,如內網 fresh DB 升級路徑):補齊缺欄。
- 新 shape (create_all 建的 dev DB):每條都是 no-op。

舊 shape 多出的 ``config`` jsonb / ``external_id`` / ``users.auth_provider_id``
保留不動 — ORM 不認識的欄位無害,砍欄位才有資料風險。
``external_identities.external_subject`` 從舊欄 ``external_id`` 回填。

Revision ID: 0040
Revises: 0039
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0040"
down_revision: Union[str, None] = "0039"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

AUTH_PROVIDER_COLUMNS = [
    ("button_text", "VARCHAR(100)"),
    ("auto_create_users", "BOOLEAN DEFAULT TRUE"),
    ("default_role", "VARCHAR(20) NOT NULL DEFAULT 'user'"),
    ("default_department_id", "INTEGER"),
    ("oidc_issuer_url", "VARCHAR(255)"),
    ("oidc_client_id", "VARCHAR(255)"),
    ("oidc_client_secret", "VARCHAR(2000)"),
    ("oidc_authorization_endpoint", "VARCHAR(255)"),
    ("oidc_token_endpoint", "VARCHAR(255)"),
    ("oidc_userinfo_endpoint", "VARCHAR(255)"),
    ("oidc_scopes", "VARCHAR(255)"),
    ("oidc_username_claim", "VARCHAR(100)"),
    ("oidc_email_claim", "VARCHAR(100)"),
    ("oidc_subject_claim", "VARCHAR(100)"),
    ("updated_at", "TIMESTAMPTZ"),
]

EXTERNAL_IDENTITY_COLUMNS = [
    ("external_subject", "VARCHAR(255)"),
    ("external_username", "VARCHAR(255)"),
    ("external_email", "VARCHAR(255)"),
    ("last_login_at", "TIMESTAMPTZ"),
]


def upgrade() -> None:
    for col, ddl in AUTH_PROVIDER_COLUMNS:
        op.execute(f"ALTER TABLE auth_providers ADD COLUMN IF NOT EXISTS {col} {ddl}")

    # FK / index 沒有 IF NOT EXISTS 語法 → DO-block guard
    op.execute(
        """
        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_constraint
                           WHERE conname = 'auth_providers_default_department_id_fkey') THEN
                ALTER TABLE auth_providers
                    ADD CONSTRAINT auth_providers_default_department_id_fkey
                    FOREIGN KEY (default_department_id)
                    REFERENCES departments(id) ON DELETE SET NULL;
            END IF;
        END $$
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_auth_providers_provider_type "
        "ON auth_providers (provider_type)"
    )

    for col, ddl in EXTERNAL_IDENTITY_COLUMNS:
        op.execute(f"ALTER TABLE external_identities ADD COLUMN IF NOT EXISTS {col} {ddl}")

    # 舊 shape 的綁定值在 external_id — 回填到 external_subject 後補 unique。
    # (新 shape DB 沒有 external_id 欄,整段 guard 掉。)
    op.execute(
        """
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM information_schema.columns
                       WHERE table_name = 'external_identities'
                         AND column_name = 'external_id') THEN
                UPDATE external_identities
                   SET external_subject = LEFT(external_id, 255)
                 WHERE external_subject IS NULL;
            END IF;
        END $$
        """
    )
    op.execute(
        """
        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_constraint
                           WHERE conname = 'uq_external_identity_subject') THEN
                ALTER TABLE external_identities
                    ADD CONSTRAINT uq_external_identity_subject
                    UNIQUE (provider_id, external_subject);
            END IF;
        END $$
        """
    )


def downgrade() -> None:
    # 只拆本 migration 自己加的東西;create_all 來源的同名欄位無從區分,
    # 一律拆掉 — downgrade 在這條鏈上僅供 fresh-DB round-trip 用。
    op.execute(
        "ALTER TABLE external_identities DROP CONSTRAINT IF EXISTS uq_external_identity_subject"
    )
    for col, _ in reversed(EXTERNAL_IDENTITY_COLUMNS):
        op.execute(f"ALTER TABLE external_identities DROP COLUMN IF EXISTS {col}")
    op.execute("DROP INDEX IF EXISTS ix_auth_providers_provider_type")
    op.execute(
        "ALTER TABLE auth_providers DROP CONSTRAINT IF EXISTS auth_providers_default_department_id_fkey"
    )
    for col, _ in reversed(AUTH_PROVIDER_COLUMNS):
        op.execute(f"ALTER TABLE auth_providers DROP COLUMN IF EXISTS {col}")
