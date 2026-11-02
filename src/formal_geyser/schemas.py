from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import AfterValidator, Field, TypeAdapter, ValidationInfo, model_validator

from formal_geyser.coordination.models import StrictModel
from formal_geyser.coordination.snapshot_limits import (
    FORMER_MAX_SNAPSHOT_FILES,
    MAX_SNAPSHOT_BYTES,
    MAX_SNAPSHOT_FILES,
)

from .paths import content_hash, lean_name, module_path, relative_path

Digest = Annotated[str, AfterValidator(content_hash)]
LeanName = Annotated[str, AfterValidator(lean_name)]
RelativePath = Annotated[str, AfterValidator(relative_path)]
ArtifactId = Annotated[str, Field(min_length=1, max_length=200)]
Text = Annotated[str, Field(min_length=1, max_length=200000)]


class SourceLocation(StrictModel):
    """An exact native literature citation."""
    literature_id: ArtifactId
    content_sha256: Digest
    locator: str = Field(min_length=1, max_length=2000)
    quotation: str | None = Field(default=None, max_length=20000)


class ObjectRecord(StrictModel):
    schema_version: Literal[1] = 1
    name: str = Field(min_length=1, max_length=300)
    version: int = Field(default=1, ge=1)
    supersedes: ArtifactId | None = None
    qualifications: list[str] = Field(default_factory=list, max_length=100, description=(
        "Author-declared scope notes for independent review against the task; "
        "not machine-validation findings or an automatic publication veto."))


class LeanMarkdownBundle(StrictModel):
    """One immutable, explicitly paired mathematical contribution."""
    snapshot: Digest
    lean_path: RelativePath
    lean_sha256: Digest
    markdown_path: RelativePath
    markdown_sha256: Digest

    @model_validator(mode="after")
    def same_stem(self):
        if not self.lean_path.endswith(".lean") or self.markdown_path != self.lean_path[:-5] + ".md":
            raise ValueError("Lean and Markdown must have the same path stem and .lean/.md extensions")
        return self


# Fields of the earlier artifact-bound record format. Records staged before the change
# keep their exact stored bytes, hashes and evidence, and a read of the store parses them
# as current records. A new record with any of these fields is invalid.
RETIRED_RECORD_FIELDS = frozenset({
    "definitions", "target_type_sha256", "statement_artifact", "statement_version",
    "candidate_module", "candidate_declaration", "native_binding", "binding_artifact",
})
STORED_RECORD = {"stored_record": True}


class MathematicalRecord(ObjectRecord):
    bundle: LeanMarkdownBundle
    environment_artifact: ArtifactId
    environment_version: int = Field(ge=1)
    entry_module: LeanName
    declaration: LeanName

    @model_validator(mode="before")
    @classmethod
    def _current_format(cls, value: Any, info: ValidationInfo) -> Any:
        if (not (info.context or {}).get("stored_record") or not isinstance(value, dict)
                or not RETIRED_RECORD_FIELDS & value.keys()):
            return value
        current = {key: item for key, item in value.items() if key not in RETIRED_RECORD_FIELDS}
        binding = value.get("native_binding")
        if (value.get("object_type") == "proof" and "statement_declaration" not in value
                and isinstance(binding, dict) and isinstance(binding.get("baseline_declaration"), str)):
            # An earlier proof named its target through the bound statement's declaration.
            current["statement_declaration"] = binding["baseline_declaration"]
        return current

    @model_validator(mode="after")
    def exact_bundle(self):
        if self.bundle.lean_path != module_path(self.entry_module):
            raise ValueError("Mathematical bundle Lean path must match its entry module")
        return self


class SourceMathematicalRecord(MathematicalRecord):
    provenance: Literal["source", "derived", "internal"] = "source"
    provenance_rationale: str = Field(default="", max_length=20000)
    sources: list[SourceLocation] = Field(default_factory=list, max_length=200)

    @model_validator(mode="after")
    def exact_sources(self):
        if self.provenance == "source" and not self.sources:
            raise ValueError("Source-backed mathematics requires source references")
        if self.provenance != "source" and not self.provenance_rationale.strip():
            raise ValueError("Derived/internal mathematics requires an explicit provenance rationale")
        return self


class DefinitionRecord(SourceMathematicalRecord):
    object_type: Literal["definition"] = "definition"
    description: Text


class StatementRecord(SourceMathematicalRecord):
    object_type: Literal["statement"] = "statement"
    statement: Text


class ProofRecord(MathematicalRecord):
    object_type: Literal["proof"] = "proof"
    # The current Library declaration this theorem proves; there is no artifact identity.
    statement_declaration: LeanName
    task_id: ArtifactId
    task_revision: int = Field(ge=1)
    snapshot: Digest | None = None


class BuildCandidateRecord(ObjectRecord):
    object_type: Literal["build-candidate"] = "build-candidate"
    environment_artifact: ArtifactId
    task_id: ArtifactId
    task_revision: int = Field(ge=1)
    snapshot: Digest | None = None
    modules: list[LeanName] = Field(min_length=1, max_length=MAX_SNAPSHOT_FILES)

    @model_validator(mode="after")
    def unique_modules(self):
        if len(self.modules) != len(set(self.modules)):
            raise ValueError("Build modules must be unique and ordered explicitly")
        return self


class CorrespondenceFinding(StrictModel):
    lean_location: str = Field(min_length=1, max_length=1000)
    markdown_location: str = Field(min_length=1, max_length=1000)
    finding: str = Field(min_length=1, max_length=4000)


class SourceCorrespondenceFinding(CorrespondenceFinding, SourceLocation):
    """Three-way findings bind the same exact native literature citation."""


class SemanticReviewEvidence(StrictModel):
    """Small exact references and findings; source bodies stay in ordinary files."""
    source_checks: list[SourceCorrespondenceFinding] = Field(default_factory=list, max_length=200)
    lean_markdown: list[CorrespondenceFinding] = Field(min_length=1, max_length=100)
    provenance_rationale: str = Field(default="", max_length=4000)


class LibraryRoot(StrictModel):
    path: str = Field(min_length=1, max_length=4000)
    manifest_sha256: Digest


class ExecutionLimits(StrictModel):
    timeout_seconds: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    output_bytes: int = Field(default=1048576, ge=1024, le=16777216)
    snapshot_bytes: int = Field(default=67108864, ge=1024, le=MAX_SNAPSHOT_BYTES)
    snapshot_files: int = Field(default=2048, ge=1, le=MAX_SNAPSHOT_FILES)

    @property
    def snapshot_file_limit(self) -> int:
        """The snapshot file cap this environment allows. An environment recorded at the former
        platform maximum asked for the largest snapshot allowed, so it gets the current maximum;
        every task pins its environment, so a replacement record would mean revising them all."""
        return MAX_SNAPSHOT_FILES if self.snapshot_files == FORMER_MAX_SNAPSHOT_FILES else self.snapshot_files


class EnvironmentRecord(ObjectRecord):
    object_type: Literal["environment"] = "environment"
    lean_executable: str = Field(min_length=1, max_length=4000)
    lean_executable_sha256: Digest
    lean_version: str = Field(min_length=1, max_length=1000)
    lake_executable_sha256: Digest | None = None
    library_roots: list[LibraryRoot] = Field(default_factory=list, max_length=100)
    # Allowed assumptions for the submitted declaration, not a module-wide audit.
    allowed_axioms: list[LeanName] = Field(default_factory=lambda: [
        "propext", "Classical.choice", "Quot.sound"
    ], max_length=200)
    local_execution: Literal["disabled", "trusted-code-only"] = "disabled"
    limits: ExecutionLimits = Field(default_factory=ExecutionLimits)

    @model_validator(mode="after")
    def no_holes(self):
        if "sorryAx" in self.allowed_axioms or any(x.endswith(".sorryAx") for x in self.allowed_axioms):
            raise ValueError("The placeholder axiom cannot be an accepted assumption")
        if len(set(self.allowed_axioms)) != len(self.allowed_axioms):
            raise ValueError("Axiom policy contains duplicate names")
        return self


DomainObject = Annotated[
    DefinitionRecord | StatementRecord | ProofRecord | BuildCandidateRecord | EnvironmentRecord,
    Field(discriminator="object_type"),
]
OBJECT_ADAPTER: TypeAdapter[DomainObject] = TypeAdapter(DomainObject)


def stored_record(data: Any) -> DomainObject:
    """Parse a record already in the store, including one staged in the earlier format."""
    return OBJECT_ADAPTER.validate_python(data, context=STORED_RECORD)


OBJECT_TYPES: dict[str, type[ObjectRecord]] = {
    "definition": DefinitionRecord, "statement": StatementRecord, "proof": ProofRecord,
    "build-candidate": BuildCandidateRecord, "environment": EnvironmentRecord,
}
APPROVAL_POLICIES = {
    "definition": "lean.definition@1", "statement": "lean.statement@1",
    "proof": "lean.proof@1", "environment": "lean.environment@1",
}


METADATA_POLICIES = {
    key: policy for key, policy in APPROVAL_POLICIES.items() if key in {"definition", "statement"}
}


class LeanTaskPayload(StrictModel):
    modules: list[LeanName] = Field(default_factory=list, max_length=MAX_SNAPSHOT_FILES)
    environment_artifact: ArtifactId | None = None
    subject_artifact: ArtifactId | None = None
    parent_task_id: ArtifactId | None = None
    parent_task_revision: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def exact_parent(self):
        if (self.parent_task_id is None) != (self.parent_task_revision is None):
            raise ValueError("A parent task requires its exact revision")
        return self
