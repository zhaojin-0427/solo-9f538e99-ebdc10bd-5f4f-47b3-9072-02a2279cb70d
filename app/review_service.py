"""Archive workflow: submission, review, supplementary tables, confirmation.

The workflow rules live here (no FastAPI types) so they can be reasoned about
and tested directly:

* a **submission** stores handoff-package version 1 plus a multi-period
  population table as revision 1 and returns the review;
* a **supplement** replaces the whole pending table, reuses the stored
  package, and bumps the revision -- it is rejected once the archive has
  been confirmed;
* a **confirmation** names the revision the caller reviewed and only
  succeeds when the contradiction has exactly one consistent column-pair
  transposition; it stores the original table, the swapped pair and the
  corrected table together and returns the corrected table.  It is rejected
  when a supplement has since been submitted (stale revision);
* every write is atomic: a failed write leaves the previous archive state
  in place and raises :class:`ArchiveWriteError`.
"""
import threading
from typing import Any, Dict, List, Optional

from .almanac import full_review, normalize_table, swap_columns
from .archive_store import ArchiveStore, ArchiveWriteError, validate_archive_id


class WorkflowError(Exception):
    """A workflow rule was violated; the archive is unchanged."""

    def __init__(self, code: str, detail: str, extra: Optional[dict] = None):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.extra = extra or {}


_LOCKS: Dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(archive_id: str) -> threading.RLock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(archive_id, threading.RLock())


class ReviewService:
    def __init__(self, store: Optional[ArchiveStore] = None):
        self.store = store or ArchiveStore()

    # -- helpers ---------------------------------------------------------
    def _load(self, archive_id: str, required: bool) -> Optional[dict]:
        archive_id = validate_archive_id(archive_id)
        record = self.store.load(archive_id)
        if record is None and required:
            raise WorkflowError(
                "archive_not_found", f"no archive with id {archive_id!r}"
            )
        return record

    @staticmethod
    def _table_for_response(table: dict) -> dict:
        return {
            "header": list(table["header"]),
            "rows": [
                {
                    "period": row["period"],
                    "values": [row["values"][code] for code in table["header"]],
                }
                for row in table["rows"]
            ],
        }

    def _require_pending(self, record: dict) -> None:
        if record["status"] == "confirmed":
            raise WorkflowError(
                "archive_confirmed",
                "this archive is confirmed; no further submissions are "
                "accepted",
            )

    def _same_package(self, stored_package: dict, submitted: Any) -> bool:
        if submitted is None:
            return True
        return submitted == stored_package

    @staticmethod
    def _install_pending(
        record: dict, revision: int, table: dict, review: dict, kind: str
    ) -> None:
        record["revision"] = revision
        record["current"] = {
            "revision": revision,
            "table": table,
            "review": review,
        }
        record["history"].append(
            {
                "revision": revision,
                "kind": kind,
                "table": table,
                "review": review,
                "swapped_pair": None,
                "corrected_table": None,
            }
        )

    # -- submission ------------------------------------------------------
    def submit(
        self,
        archive_id: str,
        package: dict,
        table_raw: Any,
        submitted_package: Any = None,
    ) -> dict:
        archive_id = validate_archive_id(archive_id)
        if submitted_package is not None and submitted_package != package:
            # The body's package *is* the package; this only guards callers
            # that pass both explicitly.
            raise WorkflowError(
                "package_mismatch", "handoff package does not match"
            )
        table = normalize_table(table_raw, package["old_districts"])
        review = full_review(package, table)
        with _lock_for(archive_id):
            if self.store.load(archive_id) is not None:
                raise WorkflowError(
                    "archive_exists",
                    f"archive {archive_id!r} already exists; submit a "
                    "supplementary table instead",
                    {"archive_id": archive_id},
                )
            record = {
                "archive_id": archive_id,
                "schema_version": 1,
                "package": package,
                "status": "pending_review",
                "revision": 1,
                "current": {
                    "revision": 1,
                    "table": table,
                    "review": review,
                },
                "history": [
                    {
                        "revision": 1,
                        "kind": "submission",
                        "table": table,
                        "review": review,
                        "swapped_pair": None,
                        "corrected_table": None,
                    }
                ],
                "confirmation": None,
            }
            self.store.save(archive_id, record)
        return {
            "archive_id": archive_id,
            "revision": 1,
            "status": "pending_review",
            "review": review,
            "table": self._table_for_response(table),
        }

    # -- supplements -----------------------------------------------------
    def supplement(
        self,
        archive_id: str,
        table_raw: Any,
        package: Any = None,
        expected_revision: Optional[int] = None,
    ) -> dict:
        archive_id = validate_archive_id(archive_id)
        with _lock_for(archive_id):
            record = self._load(archive_id, required=True)
            self._require_pending(record)
            if expected_revision is not None and (
                expected_revision != record["revision"]
            ):
                raise WorkflowError(
                    "revision_mismatch",
                    f"expected revision {expected_revision} but the pending "
                    f"revision is {record['revision']}",
                    {"revision": record["revision"]},
                )
            if not self._same_package(record["package"], package):
                raise WorkflowError(
                    "package_mismatch",
                    "a supplementary table must reuse the original "
                    "handoff package",
                    {"revision": record["revision"]},
                )
            table = normalize_table(
                table_raw, record["package"]["old_districts"]
            )
            review = full_review(record["package"], table)
            revision = record["revision"] + 1
            self._install_pending(record, revision, table, review, "supplement")
            self.store.save(archive_id, record)
        return {
            "archive_id": archive_id,
            "revision": revision,
            "status": "pending_review",
            "review": review,
            "table": self._table_for_response(table),
        }

    # -- confirmation ----------------------------------------------------
    def confirm(
        self,
        archive_id: str,
        revision: int,
        pair: Optional[Dict[str, str]] = None,
    ) -> dict:
        archive_id = validate_archive_id(archive_id)
        with _lock_for(archive_id):
            record = self._load(archive_id, required=True)
            if record["status"] == "confirmed":
                raise WorkflowError(
                    "archive_confirmed",
                    "this archive has already been confirmed",
                    {"revision": record["revision"]},
                )
            if revision != record["revision"]:
                raise WorkflowError(
                    "revision_stale",
                    f"revision {revision} is no longer the pending revision "
                    f"({record['revision']}); a supplementary table was "
                    "submitted after it was seen",
                    {"revision": record["revision"]},
                )
            current = record["current"]
            review = current["review"]
            candidates = review["candidates"]
            if not review["contradiction_found"]:
                raise WorkflowError(
                    "no_contradiction",
                    "the table passes; no contradiction was found and no "
                    "column swap is needed",
                    {"review": review},
                )
            chosen = None
            if pair is not None:
                requested = {pair.get("first"), pair.get("second")}
                match = next(
                    (
                        candidate
                        for candidate in candidates
                        if {candidate["first"], candidate["second"]}
                        == requested
                    ),
                    None,
                )
                if match is None:
                    raise WorkflowError(
                        "candidate_mismatch",
                        "the submitted pair is not a consistent column pair",
                        {"candidates": candidates},
                    )
                if len(candidates) > 1:
                    raise WorkflowError(
                        "candidate_not_unique",
                        "confirmation requires exactly one consistent column "
                        f"pair; found {len(candidates)}",
                        {"candidates": candidates, "review": review},
                    )
                chosen = match
            else:
                if not candidates:
                    raise WorkflowError(
                        "no_candidate",
                        "no single column transposition makes every period "
                        "pass; the table cannot be confirmed",
                        {"candidates": [], "review": review},
                    )
                if len(candidates) > 1:
                    raise WorkflowError(
                        "candidate_not_unique",
                        "confirmation requires exactly one consistent column "
                        f"pair; found {len(candidates)}",
                        {"candidates": candidates, "review": review},
                    )
                chosen = candidates[0]
            corrected = swap_columns(
                current["table"], chosen["first"], chosen["second"]
            )
            corrected_review = full_review(record["package"], corrected)
            if corrected_review["contradiction_found"]:
                raise WorkflowError(
                    "candidate_invalid",
                    "the selected column pair no longer passes the review",
                )
            record["status"] = "confirmed"
            record["confirmation"] = {
                "revision": revision,
                "swapped_pair": chosen,
                "original_table": current["table"],
                "corrected_table": corrected,
                "original_review": review,
                "corrected_review": corrected_review,
            }
            record["history"].append(
                {
                    "revision": revision,
                    "kind": "confirmation",
                    "table": current["table"],
                    "review": review,
                    "swapped_pair": chosen,
                    "corrected_table": corrected,
                }
            )
            self.store.save(archive_id, record)
        return {
            "archive_id": archive_id,
            "revision": revision,
            "status": "confirmed",
            "swapped_pair": chosen,
            "corrected_table": self._table_for_response(corrected),
            "review": corrected_review,
        }

    # -- retrieval -------------------------------------------------------
    def get(self, archive_id: str) -> dict:
        record = self._load(archive_id, required=True)
        response = {
            "archive_id": record["archive_id"],
            "schema_version": record["schema_version"],
            "package": record["package"],
            "status": record["status"],
            "revision": record["revision"],
            "current": {
                "revision": record["current"]["revision"],
                "table": self._table_for_response(record["current"]["table"]),
                "review": record["current"]["review"],
            },
        }
        if record["confirmation"] is not None:
            confirmation = record["confirmation"]
            response["confirmation"] = {
                "revision": confirmation["revision"],
                "swapped_pair": confirmation["swapped_pair"],
                "original_table": self._table_for_response(
                    confirmation["original_table"]
                ),
                "corrected_table": self._table_for_response(
                    confirmation["corrected_table"]
                ),
                "review": confirmation["corrected_review"],
            }
        else:
            response["confirmation"] = None
        return response

    def list_archives(self) -> List[str]:
        return self.store.list_ids()
