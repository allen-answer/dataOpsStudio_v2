from typing import Any


def encrypt(payload: Any) -> tuple[object, object | None]:
    return payload.password, payload.get("token")
