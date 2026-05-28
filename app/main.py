from __future__ import annotations

import uvicorn

from app.api.app import create_app
from app.config import load_settings

settings = load_settings()
app = create_app(settings=settings)


def main() -> None:
    uvicorn.run(
        "app.main:app",
        host=settings.api.host,
        port=settings.api.port,
        reload=False,
    )


if __name__ == "__main__":
    main()
