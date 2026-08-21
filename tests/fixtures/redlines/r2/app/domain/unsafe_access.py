from typing import Any


def read_credentials(credentials: Any, field: str) -> tuple[object, ...]:
    return (
        credentials.password,
        credentials.old_password,
        credentials.smtp_password,
        credentials.update_password,
        credentials.api_key,
        credentials.access_token,
        getattr(credentials, "token"),  # noqa: B009 - R2 getattr fixture
        getattr(credentials, "new_password"),  # noqa: B009 - R2 getattr fixture
        getattr(credentials, "clear_api_key"),  # noqa: B009 - R2 getattr fixture
        getattr(credentials, "has_stored_api_key"),  # noqa: B009 - R2 getattr fixture
        credentials.password_ref,
        credentials.api_key_secret_ref,
        credentials.clear_api_key,
        credentials.has_stored_api_key,
        credentials[field],
        getattr(credentials, field),
    )
