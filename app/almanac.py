"""Almanac table review: multi-period old-district population tables.

The almanac copies several years of data from one old-district table, so a
single pair of column labels may have been transposed.  This module provides
the pure domain logic used by the archive endpoints:

* normalising a submitted table (header + one row per period, blanks mean a
  district did not report that period);
* checking *every* old-district equation that can be tested from the
  districts that actually reported -- including equations obtained by
  combining several package relations, whose unsupported terms cancel;
* enumerating all column pairs whose transposition in every period would make
  every period pass;
* applying such a transposition to obtain the corrected table.
"""
import csv
import io
from fractions import Fraction
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .expressions import format_expression
from .rationals import nullspace, primitive_integer

# Row keys understood as the period label and as the population payload.
_PERIOD_KEYS = (
    "period",
    "year",
    "label",
    "name",
    "period_label",
    "period_id",
    "date",
)
_VALUE_KEYS = (
    "values",
    "populations",
    "data",
    "cells",
    "old_populations",
    "population",
)
_HEADER_KEYS = (
    "header",
    "columns",
    "district_codes",
    "old_districts",
)


class TableError(ValueError):
    """A submitted population table is malformed or does not fit a package."""


# --------------------------------------------------------------------------
# Table normalisation
# --------------------------------------------------------------------------
def _parse_cell(value: Any, where: str) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        raise TableError(f"{where}: booleans are not population counts")
    if isinstance(value, int):
        if value < 0:
            raise TableError(f"{where}: population must be non-negative")
        return value
    if isinstance(value, str):
        text = value.strip()
        if text == "":
            return None
        if not text.isdigit():
            raise TableError(
                f"{where}: expected a non-negative integer or a blank, "
                f"got {value!r}"
            )
        return int(text)
    raise TableError(
        f"{where}: expected a non-negative integer or a blank, got {value!r}"
    )


def _normalise_header(header: Any, old_codes: Sequence[str]) -> List[str]:
    if not isinstance(header, (list, tuple)):
        raise TableError("table header must be a list of old-district codes")
    header = list(header)
    if not all(isinstance(code, str) and code for code in header):
        raise TableError("table header entries must be non-empty strings")
    if len(set(header)) != len(header):
        raise TableError("table header contains duplicate district codes")
    expected = set(old_codes)
    actual = set(header)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        detail = []
        if missing:
            detail.append(f"missing {missing}")
        if unknown:
            detail.append(f"unknown {unknown}")
        raise TableError(
            "table header must correspond one-to-one with the package "
            f"old districts ({'; '.join(detail)})"
        )
    return header


def _cell_mapping(
    payload: Any, header: List[str], where: str
) -> Dict[str, Optional[int]]:
    if isinstance(payload, dict):
        unknown = set(payload) - set(header)
        if unknown:
            raise TableError(
                f"{where}: unknown old district(s) {sorted(unknown)}"
            )
        raw = {code: payload.get(code) for code in header}
    elif isinstance(payload, (list, tuple)):
        if len(payload) != len(header):
            raise TableError(
                f"{where}: expected {len(header)} cells, got {len(payload)}"
            )
        raw = dict(zip(header, payload))
    else:
        raise TableError(
            f"{where}: values must be a list or an object keyed by "
            "old-district code"
        )
    return {
        code: _parse_cell(raw[code], f"{where}, column {code}")
        for code in header
    }


def _normalise_row(
    row: Any, header: List[str], index: int
) -> Dict[str, Any]:
    where = f"row {index}"
    if isinstance(row, dict):
        label: Any = None
        for key in _PERIOD_KEYS:
            if key in row:
                label = row[key]
                break
        payload_key = next((k for k in _VALUE_KEYS if k in row), None)
        if payload_key is not None and row[payload_key] is not None:
            values = _cell_mapping(row[payload_key], header, where)
        else:
            # A mapping keyed directly by district code.
            values = _cell_mapping(
                {k: v for k, v in row.items() if k not in _PERIOD_KEYS},
                header,
                where,
            )
        if label is None:
            label = index + 1
    elif isinstance(row, (list, tuple)):
        items = list(row)
        if len(items) == len(header):
            label = index + 1
            cells = items
        elif len(items) == len(header) + 1:
            label = items[0]
            cells = items[1:]
        else:
            raise TableError(
                f"{where}: expected {len(header)} population cells "
                f"(optionally preceded by a period label), got {len(items)}"
            )
        values = _cell_mapping(cells, header, where)
    else:
        raise TableError(f"{where}: a table row must be an object or a list")

    if isinstance(label, (dict, list)):
        raise TableError(f"{where}: period label must be a scalar")
    if label is None or label == "":
        label = index + 1
    return {"period": label, "values": values}


def _rows_from_object(raw: Dict[str, Any]) -> List[Any]:
    if raw.get("rows") is not None:
        rows = raw["rows"]
        labels = raw.get("period_labels")
        if labels is None:
            labels = raw.get("labels")
        if not isinstance(rows, list):
            raise TableError("table rows must be a list")
        if labels is not None:
            if not isinstance(labels, list):
                raise TableError("period_labels must be a list")
            out = []
            for i, values in enumerate(rows):
                label = labels[i] if i < len(labels) else i + 1
                if isinstance(values, dict) and not any(
                    k in values for k in _VALUE_KEYS
                ):
                    values = {"period": label, "values": values}
                elif not isinstance(values, dict):
                    values = {"period": label, "values": values}
                out.append(values)
            return out
        return rows
    if raw.get("values") is not None:
        matrix = raw["values"]
        if not isinstance(matrix, list):
            raise TableError("table values must be a list of rows")
        labels = (
            raw.get("period_labels")
            or raw.get("labels")
            or raw.get("periods")
        )
        if labels is not None and not isinstance(labels, list):
            raise TableError("period labels must be a list")
        out = []
        for i, cells in enumerate(matrix):
            label = labels[i] if labels is not None and i < len(labels) else i + 1
            out.append({"period": label, "values": cells})
        return out
    if raw.get("periods") is not None:
        periods = raw["periods"]
        if not isinstance(periods, list):
            raise TableError("table periods must be a list")
        if periods and all(isinstance(p, dict) for p in periods):
            return periods
        raise TableError(
            "table.periods must be a list of row objects; supply cell "
            "values under table.rows or table.values"
        )
    raise TableError("table must contain rows, values or periods")


def _parse_csv_table(text: str, old_codes: Sequence[str]) -> Dict[str, Any]:
    records = list(csv.reader(io.StringIO(text)))
    while records and not any(cell.strip() for cell in records[-1]):
        records.pop()
    if not records:
        raise TableError("CSV table is empty")
    first = records[0]
    n = len(old_codes)
    if len(first) == n + 1:
        header = [cell.strip() for cell in first[1:]]
        has_period_column = True
    elif len(first) == n:
        header = [cell.strip() for cell in first]
        has_period_column = False
    else:
        raise TableError(
            f"CSV header has {len(first)} columns; expected {n} old-district "
            "codes (optionally preceded by a period column)"
        )
    rows = []
    for i, record in enumerate(records[1:]):
        if not any(cell.strip() for cell in record):
            continue
        if has_period_column:
            if len(record) != n + 1:
                raise TableError(
                    f"CSV row {i + 1}: expected {n + 1} columns, "
                    f"got {len(record)}"
                )
            label, cells = record[0].strip(), record[1:]
        else:
            if len(record) != n:
                raise TableError(
                    f"CSV row {i + 1}: expected {n} columns, "
                    f"got {len(record)}"
                )
            label, cells = i + 1, record
        rows.append({"period": label or i + 1, "values": cells})
    return {"header": header, "rows": rows}


def _guess_header(rows: Sequence[Any]) -> List[str]:
    first = rows[0]
    if not isinstance(first, dict):
        raise TableError(
            "a bare row list must use row objects keyed by old-district "
            "code, or supply a table object with a header"
        )
    payload = next(
        (first[k] for k in _VALUE_KEYS if first.get(k) is not None), None
    )
    if isinstance(payload, dict):
        return list(payload)
    return [
        key
        for key in first
        if key not in _PERIOD_KEYS and key not in _VALUE_KEYS
    ]


def normalize_table(raw: Any, old_codes: Sequence[str]) -> Dict[str, Any]:
    """Return ``{"header": [...], "rows": [{"period", "values": {code: v}}]}``.

    Blank cells become ``None``.  Accepted forms include an object with
    ``header`` plus ``rows`` (row objects or plain lists), an object with
    ``header`` and a ``values`` matrix, a list of row objects keyed by
    district code, and a CSV string whose first line is the header.
    """
    if isinstance(raw, str):
        raw = _parse_csv_table(raw, old_codes)

    if isinstance(raw, dict):
        header_raw = next(
            (raw[k] for k in _HEADER_KEYS if raw.get(k) is not None), None
        )
        rows_raw = _rows_from_object(raw)
        if header_raw is None:
            if not rows_raw:
                raise TableError(
                    "table header is required (a list matching the package "
                    "old districts)"
                )
            header_raw = _guess_header(rows_raw)
        header = _normalise_header(header_raw, old_codes)
    elif isinstance(raw, list):
        if not raw:
            raise TableError("table must contain at least one period row")
        header = _normalise_header(_guess_header(raw), old_codes)
        rows_raw = raw
    else:
        raise TableError(
            "table must be an object, a list of rows, or a CSV string"
        )

    if not isinstance(rows_raw, list):
        raise TableError("table rows must be a list")
    if not rows_raw:
        raise TableError("table must contain at least one period row")

    rows = []
    seen_labels = set()
    for index, row in enumerate(rows_raw):
        normalised = _normalise_row(row, header, index)
        label = normalised["period"]
        try:
            hash(label)
        except TypeError:
            label = str(label)
        while label in seen_labels:
            label = f"{label}#{index + 1}"
        seen_labels.add(label)
        normalised["period"] = label
        rows.append(normalised)
    return {"header": header, "rows": rows}


# --------------------------------------------------------------------------
# Exact old-district equation checking
# --------------------------------------------------------------------------
def _relation_basis(
    package: Dict[str, Any], header: Sequence[str]
) -> Tuple[List[List[Fraction]], List[List[int]]]:
    """Package old relations as vectors in header order (rational and int)."""
    index = {code: i for i, code in enumerate(header)}
    basis: List[List[Fraction]] = []
    for relation in package["old_relations"]:
        row = [Fraction(0) for _ in header]
        for code, coefficient in relation.items():
            row[index[code]] = Fraction(coefficient)
        basis.append(row)
    return basis, [primitive_integer(row) for row in basis]


def _check_period(
    header: Sequence[str],
    basis: Sequence[Sequence[Fraction]],
    primitive_relations: Sequence[Sequence[int]],
    row: Dict[str, Any],
) -> Dict[str, Any]:
    values = [row["values"].get(code) for code in header]
    reported = [i for i, value in enumerate(values) if value is not None]
    missing = [i for i, value in enumerate(values) if value is None]
    n = len(header)
    n_relations = len(basis)

    # Every testable relation is a rational combination of the package
    # relations whose coefficients vanish on every non-reporting district.
    # A basis of such combinations is the null space of basis[:, missing].
    restriction = [
        [basis[k][i] for k in range(n_relations)] for i in missing
    ]
    combinations = nullspace(restriction, n_relations)

    checked: List[Dict[str, Any]] = []
    seen: List[Tuple[int, ...]] = []
    for alpha in combinations:
        vector = [
            sum(alpha[k] * basis[k][i] for k in range(n_relations))
            for i in range(n)
        ]
        integers = tuple(primitive_integer(vector))
        if not any(integers) or integers in seen:
            continue
        if any(
            value != 0 and values[i] is None
            for i, value in enumerate(integers)
        ):
            continue
        seen.append(integers)
        support = [i for i, value in enumerate(integers) if value != 0]
        total = sum(integers[i] * values[i] for i in support)
        relation = {header[i]: integers[i] for i in support}
        expression = format_expression(
            {header[i]: Fraction(integers[i]) for i in support}
        )
        synthesized = not any(
            list(integers) == list(candidate)
            for candidate in primitive_relations
        )
        source = [k for k, coefficient in enumerate(alpha) if coefficient != 0]
        checked.append(
            {
                "relation": relation,
                "coefficients": dict(relation),
                "expression": expression,
                "value": int(total),
                "satisfied": total == 0,
                "synthesized": synthesized,
                "source_relation_indices": source,
            }
        )

    violations = [item for item in checked if not item["satisfied"]]
    return {
        "period": row["period"],
        "reported_districts": [header[i] for i in reported],
        "missing_districts": [header[i] for i in missing],
        "checked_relations": checked,
        "violations": violations,
        "satisfied": not violations,
    }


def review_table(
    package: Dict[str, Any], table: Dict[str, Any]
) -> Dict[str, Any]:
    """Check all testable old-district equations in every period."""
    header = _normalise_header(table["header"], package["old_districts"])
    basis, primitive_relations = _relation_basis(package, header)
    periods = [
        _check_period(header, basis, primitive_relations, row)
        for row in table["rows"]
    ]
    violations = [
        dict(violation, period=period["period"])
        for period in periods
        for violation in period["violations"]
    ]
    contradiction = bool(violations)
    if contradiction:
        bad_periods = sorted({str(v["period"]) for v in violations})
        message = (
            "contradiction found in period(s): " + ", ".join(bad_periods)
        )
    else:
        message = "no contradiction found"
    return {
        "contradiction_found": contradiction,
        "message": message,
        "periods": periods,
        "violations": violations,
    }


# --------------------------------------------------------------------------
# Column transpositions
# --------------------------------------------------------------------------
def swap_columns(
    table: Dict[str, Any], first: str, second: str
) -> Dict[str, Any]:
    """Swap two whole columns, moving blanks with the data."""
    header = list(table["header"])
    if first not in header or second not in header:
        raise TableError("swap refers to an unknown old-district code")
    if first == second:
        raise TableError("a swap needs two distinct columns")
    rows = []
    for row in table["rows"]:
        values = dict(row["values"])
        values[first], values[second] = values[second], values[first]
        rows.append({"period": row["period"], "values": values})
    return {"header": header, "rows": rows}


def candidate_pairs(
    package: Dict[str, Any], table: Dict[str, Any]
) -> List[Dict[str, str]]:
    """All column pairs whose transposition makes *every* period pass."""
    header = list(table["header"])
    candidates: List[Dict[str, str]] = []
    for i in range(len(header)):
        for j in range(i + 1, len(header)):
            swapped = swap_columns(table, header[i], header[j])
            if not review_table(package, swapped)["contradiction_found"]:
                candidates.append({"first": header[i], "second": header[j]})
    return candidates


def full_review(
    package: Dict[str, Any], table: Dict[str, Any]
) -> Dict[str, Any]:
    """Review a table and enumerate every consistent column transposition."""
    review = review_table(package, table)
    if review["contradiction_found"]:
        candidates = candidate_pairs(package, table)
        review["candidates"] = candidates
    else:
        review["message"] = "no contradiction found"
        review["candidates"] = []
    review["candidate_unique"] = len(review["candidates"]) == 1
    review["can_confirm"] = review["candidate_unique"]
    return review


def corrected_table(
    table: Dict[str, Any], pair: Dict[str, str]
) -> Dict[str, Any]:
    return swap_columns(table, pair["first"], pair["second"])
