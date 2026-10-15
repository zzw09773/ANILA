-- 由資料庫管理員在既有資料庫配置；不授與任何寫入或建表權限。
CREATE ROLE attendance_mcp_reader NOLOGIN;
GRANT CONNECT ON DATABASE attendance TO attendance_mcp_reader;
GRANT USAGE ON SCHEMA public TO attendance_mcp_reader;
GRANT SELECT ON public.attendance TO attendance_mcp_reader;
-- 另將此角色授與由院內憑證／帳號管理機制建立的服務登入者。
-- 表格欄位：anila_user_id bigint, work_date date, status text。
-- 個人資料的條件仍由服務將驗簽後 sub 綁入 WHERE，不能使用請求的 user_id。
