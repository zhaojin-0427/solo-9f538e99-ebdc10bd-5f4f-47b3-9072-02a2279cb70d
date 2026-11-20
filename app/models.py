"""Pydantic request/response models (shared by both FastAPI apps)."""
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, model_validator


# --------------------------------------------------------------------------
# Handoff package builder (/build)
# --------------------------------------------------------------------------
class DistrictInput(BaseModel):
    code: str = Field(..., min_length=1, description="District code (old or new).")
    blocks: List[str] = Field(
        ..., description="IDs of the street blocks covered by this district."
    )


class BuildRequest(BaseModel):
    old_districts: List[DistrictInput] = Field(
        ..., description="Old districts with the street blocks each covers."
    )
    new_districts: List[DistrictInput] = Field(
        ..., description="New districts with the street blocks each covers."
    )

    @model_validator(mode="after")
    def _validate(self):
        for label, districts in (
            ("old_districts", self.old_districts),
            ("new_districts", self.new_districts),
        ):
            if not districts:
                raise ValueError(f"{label} must not be empty")
            codes = [d.code for d in districts]
            if len(set(codes)) != len(codes):
                raise ValueError(f"duplicate district code in {label}")
            for d in districts:
                if len(d.blocks) != len(set(d.blocks)):
                    raise ValueError(f"duplicate block id in district {d.code}")
        return self


class NewDistrictPackage(BaseModel):
    code: str
    derivable: bool = Field(
        ..., description="Whether this new district is exactly expressible "
        "in terms of old districts."
    )
    coefficients: Dict[str, str] = Field(
        default_factory=dict,
        description="Exact fraction coefficients keyed by old district code "
        '(e.g. "2/3"); whole numbers look like "2".',
    )


class HandoffPackage(BaseModel):
    schema_version: int = 1
    old_districts: List[str]
    new_districts: List[str]
    old_relations: List[Dict[str, int]] = Field(
        ...,
        description="Exact linear equations that old-district populations "
        "must satisfy. Each entry maps old code -> integer coefficient; "
        "the weighted sum must equal 0.",
    )
    new_district_relations: List[NewDistrictPackage]

    @model_validator(mode="after")
    def _validate(self):
        old_set = set(self.old_districts)
        if len(old_set) != len(self.old_districts):
            raise ValueError("duplicate old district code in package")
        new_set = set(self.new_districts)
        if len(new_set) != len(self.new_districts):
            raise ValueError("duplicate new district code in package")
        for rel in self.old_relations:
            if not rel:
                raise ValueError("empty old relation")
            unknown = set(rel) - old_set
            if unknown:
                raise ValueError(f"old relation references unknown districts: {sorted(unknown)}")
            if all(v == 0 for v in rel.values()):
                raise ValueError("all-zero old relation")
        for item in self.new_district_relations:
            if item.code not in new_set:
                raise ValueError(f"unknown new district {item.code}")
            unknown = set(item.coefficients) - old_set
            if unknown:
                raise ValueError(
                    f"coefficient for unknown old district: {sorted(unknown)}"
                )
        if {d.code for d in self.new_district_relations} != new_set:
            raise ValueError("new_district_relations must cover every new district exactly once")
        return self


# --------------------------------------------------------------------------
# Converter (/convert)
# --------------------------------------------------------------------------
class ConvertRequest(BaseModel):
    package: HandoffPackage
    old_populations: Dict[str, int] = Field(
        ..., description="Old-district populations currently available "
        "(non-negative integers). Missing codes mean 'no data'."
    )

    @model_validator(mode="after")
    def _validate(self):
        known = set(self.package.old_districts)
        unknown = set(self.old_populations) - known
        if unknown:
            raise ValueError(f"unknown old district code(s): {sorted(unknown)}")
        negatives = [c for c, v in self.old_populations.items() if v < 0]
        if negatives:
            raise ValueError(f"populations must be non-negative integers: {negatives}")
        return self


class CheckedRelation(BaseModel):
    relation: Dict[str, int]
    expression: str
    evaluated_value: int
    satisfied: bool


class NewDistrictResult(BaseModel):
    code: str
    status: str = Field(..., description='"computed" or "missing"')
    population: Optional[int] = None
    coefficients: Dict[str, str] = Field(default_factory=dict)
    expression: Optional[str] = None
    missing_reason: Optional[str] = None


class ConvertResponse(BaseModel):
    available_old_districts: List[str]
    checked_relations: List[CheckedRelation]
    computed: List[NewDistrictResult]
    missing: List[NewDistrictResult]
    results: List[NewDistrictResult]


class ErrorResponse(BaseModel):
    error: str
    detail: Optional[str] = None


# --------------------------------------------------------------------------
# Almanac archive (/archives)
# --------------------------------------------------------------------------
# Population tables accept several equivalent shapes (objects with a header
# and rows, plain matrices, CSV text), so they stay permissive here and are
# validated by ``app.almanac.normalize_table`` against the package header.
TablePayload = Any


class SubmitRequest(BaseModel):
    package: HandoffPackage = Field(
        ...,
        description="Version-1 handoff package from the builder instance.",
    )
    table: TablePayload = Field(
        ...,
        description=(
            "Multi-period old-district population table. The header must "
            "match the package old-district codes one-to-one. Each cell is a "
            "non-negative integer or blank (blank = not reported that "
            "period). Rows may be objects ({'period': ..., 'values': "
            "[...]}) or plain lists."
        ),
    )


class SupplementRequest(BaseModel):
    table: TablePayload = Field(
        ..., description="Whole replacement table to re-review."
    )
    package: Optional[HandoffPackage] = Field(
        default=None,
        description=(
            "Optional echo of the original package. When supplied it must "
            "equal the stored package; the stored package is always used."
        ),
    )
    revision: Optional[int] = Field(
        default=None,
        description=(
            "Optional expected pending revision. Rejected when a different "
            "revision is pending."
        ),
    )


class SwapPair(BaseModel):
    first: str
    second: str


class ConfirmRequest(BaseModel):
    revision: int = Field(
        ...,
        description="Revision of the review being confirmed, as returned by "
        "the submission or supplement call.",
    )
    pair: Optional[SwapPair] = Field(
        default=None,
        description=(
            "Optional echo of the unique consistent column pair. Ignored "
            "when it matches; rejected when it differs."
        ),
    )


class RelationCheck(BaseModel):
    relation: Dict[str, int]
    coefficients: Dict[str, int]
    expression: str
    value: int
    satisfied: bool
    synthesized: bool = Field(
        ...,
        description="True when this equation is a combination of several "
        "package relations.",
    )
    source_relation_indices: List[int] = Field(default_factory=list)


class PeriodReview(BaseModel):
    period: Any
    reported_districts: List[str]
    missing_districts: List[str]
    checked_relations: List[RelationCheck]
    violations: List[RelationCheck]
    satisfied: bool


class CandidatePair(BaseModel):
    first: str
    second: str


class TableReview(BaseModel):
    contradiction_found: bool
    message: str
    periods: List[PeriodReview]
    violations: List[RelationCheck]
    candidates: List[CandidatePair] = Field(default_factory=list)
    candidate_unique: bool = False
    can_confirm: bool = False


class TableOut(BaseModel):
    header: List[str]
    rows: List[Dict[str, Any]]


class SubmitResponse(BaseModel):
    archive_id: str
    revision: int
    status: str
    review: TableReview
    table: TableOut


class ConfirmResponse(BaseModel):
    archive_id: str
    revision: int
    status: str
    swapped_pair: CandidatePair
    corrected_table: TableOut
    review: TableReview


class ArchiveResponse(BaseModel):
    archive_id: str
    schema_version: int
    package: HandoffPackage
    status: str
    revision: int
    current: Dict[str, Any]
    confirmation: Optional[Dict[str, Any]] = None


class ArchiveList(BaseModel):
    archives: List[str]


class WorkflowErrorResponse(BaseModel):
    error: str
    detail: str
    revision: Optional[int] = None
    candidates: List[CandidatePair] = Field(default_factory=list)
    review: Optional[TableReview] = None
