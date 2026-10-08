"""ASGI entry point: ``uvicorn app.main:app``."""

import logging

from app.api import create_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

app = create_app()
