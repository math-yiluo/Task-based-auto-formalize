"""Native literature is cited once, with exact file identity and semantic review."""
from copy import deepcopy
from uuid import uuid4

import pytest
from pydantic import ValidationError

from formal_geyser.coordination.config import initialize
from formal_geyser.coordination.hub import Hub
from formal_geyser.coordination.models import Command, ProjectConfig
from formal_geyser.publication import validate_semantic_review
from formal_geyser.registry import (
    EnvironmentUnavailable,
    ObjectRegistry,
    RegistryError,
    reference_ids,
)
from formal_geyser.schemas import SourceLocation, StatementRecord


@pytest.fixture
async def literature_case(tmp_path):
    initialize(tmp_path, ProjectConfig())
    hub = Hub(tmp_path)
    source = tmp_path / "source.txt"
    source.write_text("Native technical citation fixture.\n", encoding="utf-8")
    entry = await hub.execute(Command(name="literature.register", key=uuid4().hex,
        payload={"source_id": "native-source-v1", "title": "Technical source", "path": str(source), "version": "Edition one"}))
    try:
        yield hub, source, entry
    finally:
        await hub.close()


async def test_one_native_entry_supplies_exact_citation_without_source_artifact_or_review(literature_case):
    hub, source, entry = literature_case
    citation = SourceLocation(literature_id=entry["id"], content_sha256=entry["source_sha256"], locator="Page 1, definition 1")
    evidence = await ObjectRegistry(hub).source_evidence(citation)
    assert evidence["source_path"] == str(source)
    assert evidence["content_sha256"] == entry["source_sha256"]
    assert evidence["scope"] == "source-acquisition-only"
    assert await hub.db.call(lambda c: c.execute("SELECT count(*) FROM artifacts").fetchone()[0]) == 0
    assert await hub.db.call(lambda c: c.execute("SELECT count(*) FROM peer_review_receipts").fetchone()[0]) == 0
    assert await hub.db.call(lambda c: c.execute("SELECT count(*) FROM artifacts WHERE kind='lean.source'").fetchone()[0]) == 0


@pytest.mark.parametrize("failure", ["changed", "missing", "wrong_reference_hash"])
async def test_native_citation_fails_closed_on_unavailable_or_changed_bytes(literature_case, failure):
    hub, source, entry = literature_case
    citation = SourceLocation(literature_id=entry["id"], content_sha256=entry["source_sha256"], locator="Page 1")
    if failure == "changed":
        source.write_bytes(b"Different edition")
    elif failure == "missing":
        source.unlink()
    else:
        citation = citation.model_copy(update={"content_sha256": "0" * 64})
    expected = RegistryError if failure == "wrong_reference_hash" else EnvironmentUnavailable
    with pytest.raises(expected):
        await ObjectRegistry(hub).source_evidence(citation)


def test_native_source_schema_and_three_way_findings_bind_id_hash_and_locator(proof_case):
    hub, _, _ = proof_case
    citation = {"literature_id": "native-source-v1", "content_sha256": "a" * 64, "locator": "Definition 2, page 7"}
    with pytest.raises(ValidationError):
        SourceLocation(literature_id="missing-fingerprint", locator="Page 1")
    with pytest.raises(ValidationError):
        SourceLocation(source_artifact="legacy", source_version=1, **citation)
    data = deepcopy(hub.artifacts["statement-1"]["data"])
    data.update(provenance="source", sources=[citation])
    record = StatementRecord.model_validate(data)
    assert "native-source-v1" not in reference_ids(record)
    pair = {"lean_location": "Baseline.lean:1", "markdown_location": "Baseline.md:1", "finding": "The exact cited proposition and quantifiers correspond."}
    report = {"verdict": "approve", "semantic_checks": {
        "lean_markdown": [pair], "source_checks": [{**citation, **pair}]}}
    validate_semantic_review(data, report)
    report["semantic_checks"]["source_checks"][0]["content_sha256"] = "b" * 64
    with pytest.raises(RegistryError, match="every exact cited"):
        validate_semantic_review(data, report)
