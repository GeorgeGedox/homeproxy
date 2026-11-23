"""`GET /logs/stream`: the live access log tail as server-sent events. `GET /logs/files`: the
stored logs, each source downloaded whole."""

from collections.abc import AsyncIterator
from contextlib import aclosing
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse
from fastapi.sse import EventSourceResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from homeproxy.auth.middleware import session_cookie
from homeproxy.auth.routes import Runtime
from homeproxy.logs import AccessLog, LogCollector
from homeproxy.logs.files import SOURCES, Source, read_source, summary
from homeproxy.logs.stream import LogFilter, follow

router = APIRouter(prefix="/api/v1", tags=["logs"])


class StoredLog(BaseModel):
    """One stored log: its total size in bytes and when it last changed, null while empty."""

    source: Source
    size: int
    modified: datetime | None


@router.get("/logs/stream", response_class=EventSourceResponse)
async def stream(
    request: Request, runtime: Runtime, log_filter: Annotated[LogFilter, Query()]
) -> AsyncIterator[AccessLog]:
    """Send the newest kept lines that match the filters, then each new one, as JSON events.

    A reconnect starts again with the kept lines. The stream has no event ids. It ends when its
    session does, such as after a logout or a password change elsewhere.
    """
    collector: LogCollector = request.app.state.collector
    cookie = session_cookie(request.headers)
    async with aclosing(follow(collector, log_filter)) as entries:
        async for entry in entries:
            if await run_in_threadpool(runtime.authenticate, cookie) is None:
                return
            yield entry


@router.get("/logs/files")
def list_files(runtime: Runtime) -> list[StoredLog]:
    """List every source of stored logs with its size."""
    stored = []
    for source in SOURCES:
        size, modified = summary(runtime.store.root, source)
        stored.append(StoredLog(source=source, size=size, modified=modified))
    return stored


@router.get(
    "/logs/files/{source}",
    response_class=StreamingResponse,
    responses={200: {"content": {"text/plain": {}}, "description": "The log, oldest line first"}},
)
def download(runtime: Runtime, source: Source) -> StreamingResponse:
    """Download one stored log as text, its rotated files first. It holds client addresses and
    URLs as they were requested, so it is an attachment and never shown as a page."""
    return StreamingResponse(
        read_source(runtime.store.root, source),
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="homeproxy-{source}.log"'},
    )
