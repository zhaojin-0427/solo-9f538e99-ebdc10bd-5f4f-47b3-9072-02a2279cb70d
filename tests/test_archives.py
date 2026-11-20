"""End-to-end tests for the almanac archive workflow on the converter app."""
import json

import pytest
from fastapi.testclient import TestClient

from app.archive_store import ArchiveStore
from app.builder_app import app as builder_app
from app.converter_app import app as converter_app
from app.review_service import ReviewService

builder = TestClient(builder_app)
converter = TestClient(converter_app)


@pytest.fixture(autouse=True)
def archive_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("ARCHIVE_DIR", str(tmp_path / "archives"))
    return tmp_path / "archives"


def build(old, new):
    resp = builder.post("/build", json={"old_districts": old, "new_districts": new})
    assert resp.status_code == 200, resp.text
    return resp.json()


# O1 covers block a, O2 covers block b, O3 covers exactly a+b, so every
# period must satisfy O1 + O2 - O3 = 0.
def three_block_package():
    old = [
        {"code": "O1", "blocks": ["a"]},
        {"code": "O2", "blocks": ["b"]},
        {"code": "O3", "blocks": ["a", "b"]},
    ]
    return build(old, [{"code": "N1", "blocks": ["a"]}])


def table(rows, header=("O1", "O2", "O3")):
    return {
        "header": list(header),
        "rows": [{"period": p, "values": list(v)} for p, v in rows],
    }


def swap(tbl, first, second):
    """Transpose two table columns, carrying blanks along."""
    header = list(tbl["header"])
    i, j = header.index(first), header.index(second)
    rows = []
    for row in tbl["rows"]:
        values = list(row["values"])
        values[i], values[j] = values[j], values[i]
        rows.append({"period": row["period"], "values": values})
    return {"header": header, "rows": rows}


def submit(archive_id, package, tbl):
    return converter.post(
        f"/archives/{archive_id}/submissions",
        json={"package": package, "table": tbl},
    )


def supplement(archive_id, tbl, **extra):
    body = {"table": tbl}
    body.update(extra)
    return converter.post(f"/archives/{archive_id}/supplements", json=body)


def confirm(archive_id, revision, pair=None):
    body = {"revision": revision}
    if pair is not None:
        body["pair"] = pair
    return converter.post(f"/archives/{archive_id}/confirmations", json=body)


HONEST = table(
    [
        ("2018", [10, 4, 14]),
        ("2019", [31, 9, 40]),
        ("2020", [15, 4, 19]),
    ]
)


# --------------------------------------------------------------------------
# Clean table: no contradiction is reported.
# --------------------------------------------------------------------------
def test_clean_table_reports_no_contradiction():
    pkg = three_block_package()
    resp = submit(
        "clean-1", pkg, table([("2018", [4, 6, 10]), ("2019", ["7", "8", "15"])])
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["revision"] == 1
    assert body["review"]["contradiction_found"] is False
    assert body["review"]["candidates"] == []
    assert body["review"]["can_confirm"] is False
    assert "no contradiction" in body["review"]["message"]
    first = body["review"]["periods"][0]
    assert first["checked_relations"][0]["relation"] == {
        "O1": 1,
        "O2": 1,
        "O3": -1,
    }
    assert first["checked_relations"][0]["value"] == 0


def test_blank_cells_are_missing_reports():
    pkg = three_block_package()
    resp = submit(
        "clean-2",
        pkg,
        table(
            [
                ("2018", [4, 6, 10]),
                ("2019", [None, 8, None]),
                ("2020", ["", 8, 9]),
            ]
        ),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["review"]["contradiction_found"] is False
    periods = body["review"]["periods"]
    assert periods[1]["missing_districts"] == ["O1", "O3"]
    # Nothing testable when only O2 reports.
    assert periods[1]["checked_relations"] == []
    assert periods[2]["missing_districts"] == ["O1"]


def test_csv_table_with_header():
    pkg = three_block_package()
    csv_text = "period,O1,O2,O3\n2018,4,6,10\n2019,7,8,15\n"
    resp = submit("csv-1", pkg, csv_text)
    assert resp.status_code == 200, resp.text
    assert resp.json()["review"]["contradiction_found"] is False


def test_plain_matrix_table():
    pkg = three_block_package()
    resp = submit(
        "matrix-1",
        pkg,
        {"header": ["O1", "O2", "O3"], "values": [[4, 6, 10], [5, 7, 12]]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["table"]["rows"][1] == {
        "period": 2,
        "values": [5, 7, 12],
    }


def test_bare_rows_list_uses_keys_as_header():
    pkg = three_block_package()
    resp = submit(
        "rows-1",
        pkg,
        [{"year": 2018, "O1": 4, "O2": 6, "O3": 10}],
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["review"]["contradiction_found"] is False


# --------------------------------------------------------------------------
# Header must match the package old-district codes one-to-one.
# --------------------------------------------------------------------------
def test_header_must_match_package():
    pkg = three_block_package()
    resp = submit("bad-header-1", pkg, table([("2018", [1, 2])], header=("O1", "O2")))
    assert resp.status_code == 422
    assert resp.json()["error"] == "invalid_table"
    resp = submit(
        "bad-header-2", pkg, table([("2018", [1, 2, 3])], header=("O1", "O2", "O9"))
    )
    assert resp.status_code == 422
    resp = submit(
        "bad-header-3", pkg, table([("2018", [1, 2, 3])], header=("O1", "O2", "O2"))
    )
    assert resp.status_code == 422
    # Duplicate periods are disambiguated but never rejected silently.
    resp = submit("dup-period", pkg, table([("2018", [1, 2, 3]), ("2018", [2, 2, 4])]))
    assert resp.status_code == 200
    assert [p["period"] for p in resp.json()["review"]["periods"]] == [
        "2018",
        "2018#2",
    ]


def test_negative_or_non_integer_cells_rejected():
    pkg = three_block_package()
    for value in (-1, "1.5", "abc", True):
        resp = submit(f"bad-cell-{value}", pkg, table([("2018", [1, value, 3])]))
        assert resp.status_code == 422, (value, resp.text)
        assert resp.json()["error"] == "invalid_table"


# --------------------------------------------------------------------------
# Unique transposition: confirm returns the corrected table.
# --------------------------------------------------------------------------
def test_unique_swap_confirmed_returns_corrected_table():
    pkg = three_block_package()
    broken = swap(HONEST, "O1", "O3")
    resp = submit("swap-1", pkg, broken)
    body = resp.json()
    assert resp.status_code == 200
    assert body["review"]["contradiction_found"] is True
    assert body["review"]["candidates"] == [{"first": "O1", "second": "O3"}]
    assert body["review"]["can_confirm"] is True
    revision = body["revision"]

    resp = confirm("swap-1", revision, {"first": "O1", "second": "O3"})
    assert resp.status_code == 200, resp.text
    confirmed = resp.json()
    assert confirmed["status"] == "confirmed"
    assert confirmed["revision"] == revision
    assert confirmed["swapped_pair"] == {"first": "O1", "second": "O3"}
    assert confirmed["corrected_table"]["rows"] == HONEST["rows"]
    assert confirmed["review"]["contradiction_found"] is False

    stored = converter.get("/archives/swap-1")
    assert stored.status_code == 200
    archive = stored.json()
    assert archive["status"] == "confirmed"
    assert archive["confirmation"]["swapped_pair"] == {
        "first": "O1",
        "second": "O3",
    }
    # The original table is kept verbatim.
    assert archive["confirmation"]["original_table"]["rows"] == broken["rows"]
    assert archive["confirmation"]["corrected_table"]["rows"] == HONEST["rows"]
    # The archived original review still records the contradiction.
    assert archive["current"]["review"]["contradiction_found"] is True


def test_blanks_follow_columns_and_no_contradiction_when_untestable():
    pkg = three_block_package()
    # Only O2 reports: the relation cannot be tested, so there is no
    # contradiction and no candidate list.
    resp = submit("blank-1", pkg, table([("2018", [None, 5, 6])]))
    body = resp.json()
    assert body["review"]["contradiction_found"] is False
    assert body["review"]["candidates"] == []
    assert body["review"]["periods"][0]["checked_relations"] == []


def test_blanks_move_with_their_column_in_the_corrected_table():
    pkg = three_block_package()
    honest = table(
        [
            ("2018", [10, 4, 14]),
            ("2019", [None, 4, 15]),  # O1 gap; O3 cannot be re-derived
        ]
    )
    broken = swap(honest, "O1", "O3")
    assert broken["rows"][1]["values"] == [15, 4, None]
    body = submit("blank-swap", pkg, broken).json()
    assert body["review"]["candidates"] == [{"first": "O1", "second": "O3"}]
    resp = confirm("blank-swap", 1)
    assert resp.status_code == 200, resp.text
    assert resp.json()["corrected_table"]["rows"] == honest["rows"]


def test_composite_relation_is_checked():
    # Blocks: b1 covered by O1+O2, b2 covered by O2+O3+O4.
    # Package relations: O1-O2+O3 = 0 and O1-O2+O4 = 0.  When both O1 and O2
    # are missing neither package relation is testable, but their
    # combination O3 - O4 = 0 is -- a synthesized equation.
    old = [
        {"code": "O1", "blocks": ["b1"]},
        {"code": "O2", "blocks": ["b1", "b2"]},
        {"code": "O3", "blocks": ["b2"]},
        {"code": "O4", "blocks": ["b2"]},
    ]
    pkg = build(old, [{"code": "N1", "blocks": ["b1"]}])
    assert len(pkg["old_relations"]) == 2
    header = ["O1", "O2", "O3", "O4"]

    resp = submit(
        "composite-1", pkg, table([("2018", [None, None, 5, 5])], header=header)
    )
    assert resp.status_code == 200, resp.text
    period = resp.json()["review"]["periods"][0]
    assert period["satisfied"] is True
    synthesized = [c for c in period["checked_relations"] if c["synthesized"]]
    assert [c["relation"] for c in synthesized] == [{"O3": 1, "O4": -1}]

    resp = submit(
        "composite-2", pkg, table([("2018", [None, None, 5, 6])], header=header)
    )
    body = resp.json()
    assert body["review"]["contradiction_found"] is True
    assert body["review"]["violations"][0]["relation"] == {"O3": 1, "O4": -1}


def test_candidates_require_every_period_to_pass():
    pkg = three_block_package()
    # 2018 is repaired by swapping O1/O3; 2019 is corrupted differently, so
    # no single swap fixes both periods.
    broken = table(
        [
            ("2018", [14, 4, 10]),
            ("2019", [1, 2, 100]),
        ]
    )
    body = submit("multi-period-1", pkg, broken).json()
    assert body["review"]["contradiction_found"] is True
    assert body["review"]["candidates"] == []
    resp = confirm("multi-period-1", 1)
    assert resp.status_code == 409
    assert resp.json()["error"] == "no_candidate"


def test_multiple_candidates_reported_and_not_confirmable():
    # O3 covers exactly O1+O2; O4 covers a third block.  Data [0, 0, 1, 1]
    # is repaired by swapping O1/O4 or O2/O4 -- two consistent pairs, so
    # confirmation must be refused and both pairs listed.
    old = [
        {"code": "O1", "blocks": ["a"]},
        {"code": "O2", "blocks": ["b"]},
        {"code": "O3", "blocks": ["a", "b"]},
        {"code": "O4", "blocks": ["c"]},
    ]
    pkg = build(old, [{"code": "N1", "blocks": ["a"]}])
    body = submit(
        "multi-cand-2",
        pkg,
        table([("2018", [0, 0, 1, 1])], header=("O1", "O2", "O3", "O4")),
    ).json()
    assert body["review"]["contradiction_found"] is True
    pairs = {
        frozenset((c["first"], c["second"]))
        for c in body["review"]["candidates"]
    }
    assert pairs == {frozenset(("O1", "O4")), frozenset(("O2", "O4"))}
    assert body["review"]["candidate_unique"] is False
    assert body["review"]["can_confirm"] is False
    resp = confirm("multi-cand-2", 1)
    assert resp.status_code == 409
    error = resp.json()
    assert error["error"] == "candidate_not_unique"
    assert len(error["candidates"]) == 2


def test_every_listed_candidate_really_fixes_the_table():
    # Property check: whatever pairs the enumeration returns, transposing
    # each one must make every period pass.
    old = [
        {"code": "O1", "blocks": ["a"]},
        {"code": "O2", "blocks": ["b"]},
        {"code": "O3", "blocks": ["a", "b"]},
        {"code": "O4", "blocks": ["c"]},
    ]
    pkg = build(old, [{"code": "N1", "blocks": ["a"]}])
    broken = table([("2018", [0, 0, 1, 1])], header=("O1", "O2", "O3", "O4"))
    body = submit("cand-check", pkg, broken).json()
    assert body["review"]["candidates"]
    for candidate in body["review"]["candidates"]:
        fixed = swap(broken, candidate["first"], candidate["second"])
        resp = submit(f"cand-check-{candidate['first']}", pkg, fixed)
        assert resp.json()["review"]["contradiction_found"] is False


def test_confirm_requires_a_consistent_pair():
    pkg = three_block_package()
    submit("wrong-pair-1", pkg, swap(HONEST, "O1", "O3"))
    resp = confirm("wrong-pair-1", 1, {"first": "O1", "second": "O2"})
    assert resp.status_code == 409
    assert resp.json()["error"] == "candidate_mismatch"


def test_confirm_clean_table_rejected():
    pkg = three_block_package()
    submit("clean-confirm-1", pkg, HONEST)
    resp = confirm("clean-confirm-1", 1)
    assert resp.status_code == 409
    assert resp.json()["error"] == "no_contradiction"


# --------------------------------------------------------------------------
# Supplements and revision races.
# --------------------------------------------------------------------------
def test_supplement_replaces_table_and_bumps_revision():
    pkg = three_block_package()
    submit("supp-1", pkg, swap(HONEST, "O1", "O3"))
    resp = supplement("supp-1", HONEST)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["revision"] == 2
    assert body["review"]["contradiction_found"] is False
    assert body["review"]["candidates"] == []
    stored = converter.get("/archives/supp-1").json()
    assert stored["revision"] == 2
    assert stored["current"]["table"]["rows"] == HONEST["rows"]


def test_supplement_expected_revision_is_enforced():
    pkg = three_block_package()
    submit("supp-rev-1", pkg, HONEST)
    resp = supplement("supp-rev-1", HONEST, revision=1)
    assert resp.status_code == 200
    resp = supplement("supp-rev-1", HONEST, revision=1)
    assert resp.status_code == 409
    assert resp.json()["error"] == "revision_mismatch"
    assert resp.json()["revision"] == 2


def test_supplement_after_confirmation_rejected():
    pkg = three_block_package()
    submit("race-1", pkg, swap(HONEST, "O1", "O3"))
    assert confirm("race-1", 1).status_code == 200
    resp = supplement("race-1", HONEST)
    assert resp.status_code == 409
    assert resp.json()["error"] == "archive_confirmed"
    # The confirmed archive is unchanged.
    stored = converter.get("/archives/race-1").json()
    assert stored["status"] == "confirmed"
    assert stored["revision"] == 1


def test_confirmation_after_supplement_is_stale():
    pkg = three_block_package()
    submit("race-2", pkg, swap(HONEST, "O1", "O3"))
    supplement("race-2", HONEST)
    # Revision 1 was superseded by the supplement: it can no longer be
    # confirmed.
    resp = confirm("race-2", 1)
    assert resp.status_code == 409
    assert resp.json()["error"] == "revision_stale"
    assert resp.json()["revision"] == 2
    # Revision 2 is clean.
    resp = confirm("race-2", 2)
    assert resp.status_code == 409
    assert resp.json()["error"] == "no_contradiction"


def test_supplement_package_must_match_original():
    pkg = three_block_package()
    submit("pkg-1", pkg, swap(HONEST, "O1", "O3"))
    other = build(
        [{"code": "O1", "blocks": ["a"]}, {"code": "O2", "blocks": ["b"]}],
        [{"code": "N1", "blocks": ["a"]}],
    )
    resp = supplement("pkg-1", HONEST, package=other)
    assert resp.status_code == 409
    assert resp.json()["error"] == "package_mismatch"
    # Echoing the original package is accepted.
    resp = supplement("pkg-1", HONEST, package=pkg)
    assert resp.status_code == 200


def test_duplicate_submission_rejected():
    pkg = three_block_package()
    assert submit("dup-1", pkg, HONEST).status_code == 200
    resp = submit("dup-1", pkg, HONEST)
    assert resp.status_code == 409
    assert resp.json()["error"] == "archive_exists"


def test_unknown_archive_is_404():
    resp = converter.get("/archives/nope")
    assert resp.status_code == 404
    assert resp.json()["error"] == "archive_not_found"
    resp = supplement("nope", HONEST)
    assert resp.status_code == 404


def test_archive_ids_are_filesystem_safe():
    pkg = three_block_package()
    resp = converter.get("/archives/..%2Fescape")
    assert resp.status_code in (400, 404)
    resp = converter.post(
        "/archives/bad%20id/submissions",
        json={"package": pkg, "table": HONEST},
    )
    assert resp.status_code in (400, 404)


def test_archives_listed():
    pkg = three_block_package()
    submit("listed-1", pkg, HONEST)
    submit("listed-2", pkg, HONEST)
    assert converter.get("/archives").json()["archives"] == [
        "listed-1",
        "listed-2",
    ]


# --------------------------------------------------------------------------
# Persistence across restarts, and failed writes.
# --------------------------------------------------------------------------
def test_archive_survives_restart(tmp_path):
    pkg = three_block_package()
    broken = swap(HONEST, "O1", "O3")
    submit("persist-1", pkg, broken)
    assert confirm("persist-1", 1).status_code == 200

    # A fresh service over the same directory (simulates a process restart).
    fresh = ReviewService(ArchiveStore(str(tmp_path / "archives")))
    record = fresh.get("persist-1")
    assert record["status"] == "confirmed"
    assert record["confirmation"]["swapped_pair"] == {
        "first": "O1",
        "second": "O3",
    }
    assert record["confirmation"]["original_table"]["rows"] == broken["rows"]
    assert record["confirmation"]["corrected_table"]["rows"] == HONEST["rows"]


def test_failed_write_keeps_previous_state(tmp_path):
    pkg = three_block_package()
    submit("write-1", pkg, swap(HONEST, "O1", "O3"))
    before = converter.get("/archives/write-1").json()

    from app.archive_store import ArchiveWriteError

    class FailingStore(ArchiveStore):
        def save(self, archive_id, record):
            raise ArchiveWriteError("disk full")

    service = ReviewService(FailingStore(str(tmp_path / "archives")))
    with pytest.raises(ArchiveWriteError):
        service.confirm("write-1", 1)

    after = converter.get("/archives/write-1").json()
    assert after == before
    assert json.dumps(after, sort_keys=True) == json.dumps(before, sort_keys=True)
    # And the archive can still be confirmed afterwards.
    assert confirm("write-1", 1).status_code == 200


def test_original_convert_still_works():
    pkg = three_block_package()
    resp = converter.post(
        "/convert",
        json={
            "package": pkg,
            "old_populations": {"O1": 4, "O2": 6, "O3": 10},
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["computed"][0]["population"] == 4
