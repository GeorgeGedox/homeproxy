"""The built dashboard: static files, with `index.html` for every other non-API path."""

import logging
from pathlib import Path

from fastapi import FastAPI
from starlette.exceptions import HTTPException
from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

from homeproxy.auth.middleware import is_api_path

logger = logging.getLogger(__name__)

UI_DIR = Path("/opt/homeproxy/ui")
"""Where the image puts the output of `nuxt generate`."""


class SinglePageApp(StaticFiles):
    """Static files where an unknown path gets `index.html`, so deep links reach the client
    router. An `/api` path never does: it stays a JSON 404."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        """Serve the file at `path`, or `index.html` when there is none."""
        if is_api_path(scope["path"]):
            raise HTTPException(status_code=404)
        try:
            return await super().get_response(path, scope)
        except HTTPException as error:
            if error.status_code != 404:
                raise
        return await super().get_response("index.html", scope)


def mount_ui(app: FastAPI, directory: Path = UI_DIR) -> None:
    """Serve the dashboard from `directory` for every path no API route matches.

    It is the router's fallback, not a mount, so a known API path with the wrong method still
    gets 405. Without the directory (unit tests, the dev stack) only the API is served.
    """
    if not directory.is_dir():
        logger.info("no dashboard UI at %s, serving the API only", directory)
        return
    app.router.default = SinglePageApp(directory=directory)
