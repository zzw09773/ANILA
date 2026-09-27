"""Admin-only 治理端點的新家(W3-3⑦ / P3.3 起)。

既有的 admin 面散在 ``api/`` 根目錄(``alerts.py``、``audit_logs.py`` …),
不在本包一次搬 —— 那是純粹的位置搬移,審 diff 的成本遠高於收益。新的
admin-only 端點放這裡。
"""

from app.api.admin.backup_status import router as backup_status_router
from app.api.admin.health_overview import router as health_overview_router
from app.api.admin.feedback import router as feedback_router

__all__ = ["backup_status_router", "health_overview_router", "feedback_router"]
