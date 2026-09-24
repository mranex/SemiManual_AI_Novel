"""FastAPI adapter: the application boundary remains the only domain authority."""

from __future__ import annotations

import asyncio
import json
import queue
import threading
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from novel_ai.application.bootstrap import bootstrap
from novel_ai.application.commands import ApplicationCommands, ApplicationError, Command
from novel_ai.application.queries import ProjectQueries, QueryError
from novel_ai.config import AppConfig, get_config
from novel_ai.core.models import RewriteSectionRequest
from novel_ai.web.dto import (
    GENERATION_NAMES, REQUEST_MODELS, ErrorBody, QueryBody, ResultBody,
)


ERROR_STATUS = {
    "invalid_input": 422, "invalid_project_id": 422, "invalid_artifact_id": 422,
    "invalid_chapter_id": 422, "invalid_operation_id": 422,
    "missing_base_revision": 428, "invalid_base_ref": 422,
    "unknown_command": 404, "project_not_found": 404,
    "stale_candidate": 409, "stale_dependency": 409, "operation_conflict": 409,
    "retry_result_unavailable": 409, "recovery_required": 423,
    "llm_unavailable": 503, "configuration_error": 503,
}


class ProjectLocks:
    """One writer per project in this single worker process."""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}

    def for_project(self, project_id: str | None) -> threading.Lock:
        key = project_id or "__global__"
        with self._guard:
            return self._locks.setdefault(key, threading.Lock())


def _error(exc: ApplicationError | QueryError) -> JSONResponse:
    if isinstance(exc, ApplicationError):
        data = exc.as_dict()
    else:
        data = ErrorBody(code=exc.code, message=str(exc)).model_dump()
    return JSONResponse(status_code=ERROR_STATUS.get(exc.code, 400), content=data)


def _sse(kind: str, data: dict[str, Any]) -> bytes:
    return f"event: {kind}\ndata: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n".encode("utf-8")


def create_app(
    config: AppConfig | None = None, *, commands: ApplicationCommands | None = None,
    dev_origins: tuple[str, ...] = ("http://127.0.0.1:5173", "http://localhost:5173"),
    frontend_dist: Path | None = None,
) -> FastAPI:
    resolved = config or get_config()
    if commands is None:
        deps = bootstrap(resolved)
        commands = ApplicationCommands(resolved, client=deps.llm_client, registry=deps.registry)
    queries = ProjectQueries(resolved)
    locks = ProjectLocks()
    app = FastAPI(title="Manual AI Novel local API", version="1.0.0",
                  responses={status: {"model": ErrorBody} for status in (400, 404, 409, 422, 423, 428, 503)})
    app.state.config = resolved
    app.state.commands = commands
    app.state.queries = queries
    app.add_middleware(CORSMiddleware, allow_origins=list(dev_origins), allow_methods=["GET", "POST"], allow_headers=["Content-Type"])

    @app.exception_handler(ApplicationError)
    async def application_error(_request: Request, exc: ApplicationError):
        return _error(exc)

    @app.exception_handler(QueryError)
    async def query_error(_request: Request, exc: QueryError):
        return _error(exc)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, exc: RequestValidationError):
        # Pydantic's raw `input` could contain author prose or secrets.
        issues = [{"path": ".".join(map(str, e["loc"])), "code": e["type"], "message": "Input không hợp lệ."} for e in exc.errors()]
        return JSONResponse(status_code=422, content=ErrorBody(code="invalid_input", message="HTTP input không hợp lệ.", issues=issues).model_dump())

    def query_result(result: Any) -> QueryBody:
        return QueryBody.model_validate(asdict(result))

    @app.get("/api/v1/projects", response_model=QueryBody)
    def list_projects() -> QueryBody:
        return query_result(queries.list_projects())

    @app.get("/api/v1/projects/{project_id}", response_model=QueryBody)
    def open_project(project_id: str) -> QueryBody:
        return query_result(queries.open_project(project_id))

    @app.get("/api/v1/projects/{project_id}/foundation", response_model=QueryBody)
    def foundation(project_id: str) -> QueryBody:
        return query_result(queries.foundation(project_id))

    @app.get("/api/v1/projects/{project_id}/planning", response_model=QueryBody)
    def planning(project_id: str) -> QueryBody:
        return query_result(queries.planning(project_id))

    @app.get("/api/v1/projects/{project_id}/tree", response_model=QueryBody)
    def tree(project_id: str) -> QueryBody:
        return query_result(queries.tree(project_id))

    @app.get("/api/v1/projects/{project_id}/arbiter", response_model=QueryBody)
    def arbiter(project_id: str) -> QueryBody:
        return query_result(queries.arbiter(project_id))

    @app.get("/api/v1/projects/{project_id}/status", response_model=QueryBody)
    def status(project_id: str, workspace: str = "", chapter_id: str | None = None) -> QueryBody:
        return query_result(queries.status(project_id, workspace=workspace, chapter_id=chapter_id))

    @app.get("/api/v1/projects/{project_id}/artifacts/{artifact_id}", response_model=QueryBody)
    def artifact(project_id: str, artifact_id: str, audience: str = Query("author", pattern="^(author|writer)$")) -> QueryBody:
        return query_result(queries.artifact(project_id, artifact_id, audience=audience))

    @app.get("/api/v1/projects/{project_id}/chapters/{chapter_id}", response_model=QueryBody)
    def chapter(project_id: str, chapter_id: str, audience: str = Query("author", pattern="^(author|writer)$")) -> QueryBody:
        return query_result(queries.chapter(project_id, chapter_id, audience=audience))

    @app.get("/api/v1/projects/{project_id}/recovery", response_model=QueryBody)
    def recovery(project_id: str) -> QueryBody:
        return query_result(queries.recovery(project_id))

    @app.get("/api/v1/projects/{project_id}/revision", response_model=QueryBody)
    def revision(project_id: str) -> QueryBody:
        return query_result(queries.revision(project_id))

    @app.get("/api/v1/projects/{project_id}/chapters/{chapter_id}/latest-writer-operation", response_model=QueryBody)
    def latest_writer_operation(project_id: str, chapter_id: str) -> QueryBody:
        return query_result(queries.latest_writer_operation(project_id, chapter_id))

    def command_from(name: str, body: Any) -> Command:
        params = body.params.model_dump(exclude_unset=True)
        if name == "rewrite_section":
            params["request"] = RewriteSectionRequest.model_validate(params["request"])
        return Command(name=name, project_id=body.project_id, operation_id=body.operation_id,
                       params=params, base_ref=body.base_ref,
                       expected_revision=body.expected_revision,
                       expected_fingerprint=body.expected_fingerprint,
                       audience=body.audience, stream=body.stream, attempt=body.attempt,
                       version=body.version)

    def make_json(name: str, request_model: type):
        def endpoint(body: request_model) -> ResultBody:
            command = command_from(name, body)
            with locks.for_project(command.project_id):
                return ResultBody.model_validate(asdict(commands.execute(command)))
        endpoint.__name__ = f"command_{name}"
        endpoint.__annotations__["body"] = request_model
        return endpoint

    def make_stream(name: str, request_model: type):
        async def endpoint(body: request_model, request: Request) -> StreamingResponse:
            command = command_from(name, body)
            # A bounded bridge gives the synchronous callback real backpressure.
            # The producer owns the project lock until execute fully returns.
            async def frames():
                outbound: queue.Queue[tuple[str, dict[str, Any]] | None] = queue.Queue(maxsize=32)
                stopped = threading.Event()

                def put(item: tuple[str, dict[str, Any]] | None) -> None:
                    while not stopped.is_set():
                        try:
                            outbound.put(item, timeout=0.1)
                            return
                        except queue.Full:
                            continue
                    raise StreamCancelled()

                def producer() -> None:
                    try:
                        with locks.for_project(command.project_id):
                            result = commands.execute(command, on_event=lambda event: put(("generation", event.as_dict())))
                        put(("result", ResultBody.model_validate(asdict(result)).model_dump()))
                    except StreamCancelled:
                        pass
                    except ApplicationError as exc:
                        if not stopped.is_set():
                            put(("error", exc.as_dict()))
                    except Exception:
                        if not stopped.is_set():
                            put(("error", ErrorBody(code="internal_error", message="Generation thất bại; kiểm tra state.", operation_id=command.operation_id).model_dump()))
                    finally:
                        try:
                            outbound.put_nowait(None)
                        except queue.Full:
                            pass

                worker = threading.Thread(target=producer, name=f"generation-{name}")
                worker.start()
                try:
                    while True:
                        if await request.is_disconnected():
                            break
                        try:
                            item = await asyncio.to_thread(outbound.get, True, 0.1)
                        except queue.Empty:
                            if not worker.is_alive() and outbound.empty():
                                break
                            continue
                        if item is None:
                            break
                        yield _sse(*item)
                finally:
                    stopped.set()
                    # A request owns its producer. Provider I/O may take until
                    # its configured timeout, but no thread is detached.
                    await asyncio.to_thread(worker.join)

            return StreamingResponse(frames(), media_type="text/event-stream", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
        endpoint.__name__ = f"generation_{name}"
        endpoint.__annotations__["body"] = request_model
        return endpoint

    for name, model in REQUEST_MODELS.items():
        if name in GENERATION_NAMES:
            app.add_api_route(f"/api/v1/generations/{name}", make_stream(name, model), methods=["POST"],
                              tags=["generation"], responses={422: {"model": ErrorBody}})
        else:
            app.add_api_route(f"/api/v1/commands/{name}", make_json(name, model), methods=["POST"],
                              response_model=ResultBody, tags=["commands"], responses={422: {"model": ErrorBody}})

    dist = frontend_dist or (resolved.repo_root / "frontend" / "dist")
    if dist.is_dir():
        app.mount("/", StaticFiles(directory=dist, html=True), name="frontend")
    return app


class StreamCancelled(BaseException):
    """Stop at the next application callback after HTTP disconnect."""
