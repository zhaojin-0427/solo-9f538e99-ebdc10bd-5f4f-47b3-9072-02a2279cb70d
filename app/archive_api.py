"""FastAPI routes for the almanac archive workflow.

Mounted on the converter instance so that everything is reachable from the
converter's ``/docs``.  The pre-existing ``/convert`` endpoint is untouched.
"""
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from .almanac import TableError
from .archive_store import ArchiveWriteError, validate_archive_id
from .models import (
    ArchiveList,
    ArchiveResponse,
    ConfirmRequest,
    ConfirmResponse,
    SubmitRequest,
    SubmitResponse,
    SupplementRequest,
    WorkflowErrorResponse,
)
from .review_service import ReviewService, WorkflowError

_STATUS = {
    "archive_not_found": 404,
    "archive_exists": 409,
    "archive_confirmed": 409,
    "revision_mismatch": 409,
    "revision_stale": 409,
    "package_mismatch": 409,
    "candidate_not_unique": 409,
    "candidate_mismatch": 409,
    "no_candidate": 409,
    "candidate_invalid": 409,
    "no_contradiction": 409,
}


def _error(status: int, code: str, detail: str, **extra) -> JSONResponse:
    payload = WorkflowErrorResponse(
        error=code,
        detail=detail,
        revision=extra.get("revision"),
        candidates=extra.get("candidates", []),
        review=extra.get("review"),
    )
    return JSONResponse(status_code=status, content=payload.model_dump())


def _invalid_id(exc: ValueError) -> JSONResponse:
    return _error(400, "invalid_archive_id", str(exc))


def register_archive_routes(app: FastAPI, service: ReviewService) -> None:
    @app.exception_handler(WorkflowError)
    def _workflow_error_handler(_request, exc: WorkflowError):
        return _error(
            _STATUS.get(exc.code, 409), exc.code, exc.detail, **exc.extra
        )

    @app.exception_handler(ArchiveWriteError)
    def _write_error_handler(_request, exc: ArchiveWriteError):
        return _error(
            500, "archive_write_failed", f"archive not modified: {exc}"
        )

    @app.exception_handler(TableError)
    def _table_error_handler(_request, exc: TableError):
        return _error(422, "invalid_table", str(exc))

    @app.get(
        "/archives",
        response_model=ArchiveList,
        tags=["archive"],
        summary="List archive numbers",
    )
    def list_archives() -> ArchiveList:
        return ArchiveList(archives=service.list_archives())

    @app.post(
        "/archives/{archive_id}/submissions",
        response_model=SubmitResponse,
        responses={
            400: {"model": WorkflowErrorResponse},
            404: {"model": WorkflowErrorResponse},
            409: {"model": WorkflowErrorResponse},
            422: {"model": WorkflowErrorResponse},
            500: {"model": WorkflowErrorResponse},
        },
        tags=["archive"],
        summary="Submit handoff package v1 and a multi-period table for review",
    )
    def submit(archive_id: str, request: SubmitRequest):
        try:
            validate_archive_id(archive_id)
        except ValueError as exc:
            return _invalid_id(exc)
        return service.submit(
            archive_id, request.package.model_dump(), request.table
        )

    @app.post(
        "/archives/{archive_id}/supplements",
        response_model=SubmitResponse,
        responses={
            400: {"model": WorkflowErrorResponse},
            404: {"model": WorkflowErrorResponse},
            409: {"model": WorkflowErrorResponse},
            422: {"model": WorkflowErrorResponse},
            500: {"model": WorkflowErrorResponse},
        },
        tags=["archive"],
        summary="Replace the pending table with a supplementary one and re-review",
    )
    def supplement(archive_id: str, request: SupplementRequest):
        try:
            validate_archive_id(archive_id)
        except ValueError as exc:
            return _invalid_id(exc)
        return service.supplement(
            archive_id,
            request.table,
            package=request.package.model_dump() if request.package else None,
            expected_revision=request.revision,
        )

    @app.post(
        "/archives/{archive_id}/confirmations",
        response_model=ConfirmResponse,
        responses={
            400: {"model": WorkflowErrorResponse},
            404: {"model": WorkflowErrorResponse},
            409: {"model": WorkflowErrorResponse},
            422: {"model": WorkflowErrorResponse},
            500: {"model": WorkflowErrorResponse},
        },
        tags=["archive"],
        summary="Confirm the unique column swap and archive the corrected table",
    )
    def confirm(archive_id: str, request: ConfirmRequest):
        try:
            validate_archive_id(archive_id)
        except ValueError as exc:
            return _invalid_id(exc)
        return service.confirm(
            archive_id,
            request.revision,
            pair=request.pair.model_dump() if request.pair else None,
        )

    @app.get(
        "/archives/{archive_id}",
        response_model=ArchiveResponse,
        responses={
            404: {"model": WorkflowErrorResponse},
            500: {"model": WorkflowErrorResponse},
        },
        tags=["archive"],
        summary="Fetch an archived review and any confirmed correction",
    )
    def get_archive(archive_id: str):
        try:
            validate_archive_id(archive_id)
        except ValueError as exc:
            return _invalid_id(exc)
        return service.get(archive_id)
