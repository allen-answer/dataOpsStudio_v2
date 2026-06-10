from fastapi import APIRouter

from app.api.routes.admin import router as admin_router
from app.api.routes.core import router as core_router

router = APIRouter()
router.include_router(core_router)
router.include_router(admin_router)

__all__ = ["router"]
