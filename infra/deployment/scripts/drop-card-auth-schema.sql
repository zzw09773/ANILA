-- ============================================================================
-- drop-card-auth-schema.sql
-- ----------------------------------------------------------------------------
-- 從 prod-intranet-card 切到「純帳密版」(prod-public-passwd / prod-military-passwd
-- / dev-public / dev-military)時用的 one-time DB cleanup。
--
-- 當你之前跑過 prod-intranet-card stack(SSO + 中科院 PKI 卡),db volume 內會
-- 留下 card auth fork 專屬的 schema:
--   - auth_providers          表(OIDC provider 設定 + envelope-encrypted secret)
--   - external_identities     表(SSO 帳號 ↔ users 綁定)
--   - users.local_password_disabled  column(SSO-only 切換 flag)
--
-- 切到純帳密版後,新版 csp 的 ORM 不認識這些 schema(extra column / table 不會
-- 破讀寫,但下次 alembic upgrade 可能跳 conflict)。本 script 一次清乾淨。
--
-- ⚠️  注意:
--   - 會丟掉 auth_providers 內的 OIDC 設定(client_id / encrypted secret)。
--   - 會丟掉 external_identities 內的 SSO 帳號綁定 row。
--   - 會丟掉 users.local_password_disabled flag(card-only user 變成普通 user,
--     但他們沒設過密碼,實際也無路徑登入 — 需要 admin 之後手動給設密碼)。
--   - 不會丟 users 表內的 row 本身(姓名 / email / role 等都保留)。
--
-- 跑之前建議先 pg_dump 備份:
--   docker compose exec -T csp-db pg_dump -U csp_app csp > csp-backup-$(date +%F).sql
--
-- 跑法:
--   docker compose exec -T csp-db psql -U csp_app -d csp \
--     < scripts/drop-card-auth-schema.sql
--
-- 跑完 csp 不用重啟,因為 ORM 本來就不認得這些 schema。
-- ============================================================================

BEGIN;

-- 1. 先刪外鍵連結:external_identities 指向 auth_providers + users。
DROP TABLE IF EXISTS external_identities CASCADE;

-- 2. 刪 auth_providers 主表。
DROP TABLE IF EXISTS auth_providers CASCADE;

-- 3. 移除 users.local_password_disabled column。
ALTER TABLE users DROP COLUMN IF EXISTS local_password_disabled;

-- 4. 顯示剩下 users 的 active row 數(sanity check,不該因為這個 cleanup 變動)。
SELECT COUNT(*) AS users_after_cleanup FROM users;

COMMIT;
