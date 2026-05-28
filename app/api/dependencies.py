from __future__ import annotations

from typing import cast

from fastapi import Request

from app.api.errors import ApiError
from app.api.security import CurrentUser
from app.api.services import ApiServices


def services_from(request: Request) -> ApiServices:
    services = getattr(request.app.state, "services", None)
    if services is None:
        raise ApiError(500, "internal_error", "Request failed")
    return cast(ApiServices, services)


def current_user_from(request: Request) -> CurrentUser:
    user = getattr(request.state, "user", None)
    if not isinstance(user, CurrentUser):
        raise ApiError(401, "unauthorized", "Authentication required")
    return user


def request_id_from(request: Request) -> str | None:
    value = getattr(request.state, "request_id", None)
    return str(value) if value is not None else None
