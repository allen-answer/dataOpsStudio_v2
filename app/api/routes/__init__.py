from fastapi import APIRouter

from app.api.routes.account import router as account_router
from app.api.routes.admin import router as admin_router
from app.api.routes.compare_result_inputs import router as compare_result_inputs_router
from app.api.routes.core import router as core_router
from app.api.routes.sessions import router as sessions_router

router = APIRouter()
router.include_router(core_router)
router.include_router(account_router)
router.include_router(admin_router)
router.include_router(sessions_router)
router.include_router(compare_result_inputs_router)

__all__ = ["router"]
