"""Supported local single-worker startup."""

from __future__ import annotations

import uvicorn

from novel_ai.web.app import create_app


def main() -> None:
    uvicorn.run(create_app(), host="127.0.0.1", port=8000, workers=1)


if __name__ == "__main__":
    main()
