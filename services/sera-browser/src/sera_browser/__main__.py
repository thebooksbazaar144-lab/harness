"""Entry point: `python -m sera_browser`."""
from __future__ import annotations

import sys

from .api.app import create_app


def main() -> int:
    import uvicorn

    from .config import Settings

    settings = Settings()
    app = create_app(settings)
    uvicorn.run(
        app,
        host=settings.api_host,
        port=settings.api_port,
        log_level=settings.log_level.lower(),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
