"""Reuse immutable inputs only within one rechecked metadata resolution."""
import asyncio
from collections import Counter

import pytest

from formal_geyser.registry import (
    ApprovalRequired,
    EnvironmentUnavailable,
    ObjectRegistry,
)
from formal_geyser.schemas import SourceLocation
from formal_geyser.verification import ProofVerifier
from tests.conftest import cite


def count_artifacts(hub, monkeypatch):
    counts = Counter()
    original = hub.artifact
    async def read(key):
        counts[key] += 1
        return await original(key)
    monkeypatch.setattr(hub, "artifact", read)
    return counts


async def test_scope_reuses_reads_rechecks_and_does_not_leak_mutation(proof_case, monkeypatch):
    hub, _, _ = proof_case
    counts = count_artifacts(hub, monkeypatch)
    registry = ObjectRegistry(hub)
    async with registry.verified_reads():
        record, artifact = await registry.get("environment-1", approved=True)
        name = record.name
        artifact["data"]["name"] = "Mutated returned copy"
        async with registry.verified_reads():
            again, _ = await registry.get("environment-1", approved=True)
        assert again.name == name
        assert counts["environment-1"] == 1
    assert counts["environment-1"] == 2
    await registry.get("environment-1")
    assert counts["environment-1"] == 3
    assert registry._read_cache() is None


@pytest.mark.parametrize("change", ["body", "approval"])
async def test_postcheck_detects_changed_identity_or_approval(proof_case, change):
    hub, _, _ = proof_case
    registry = ObjectRegistry(hub)
    with pytest.raises(EnvironmentUnavailable, match="input changed"):
        async with registry.verified_reads():
            await registry.get("environment-1", approved=True)
            if change == "body":
                hub.artifacts["environment-1"]["data"]["name"] = "A different registration"
            else:
                hub.artifacts["environment-1"]["evidence"].append(
                    {"policy": "lean.environment@1", "verdict": "rejected"})
    assert registry._read_cache() is None
    if change == "approval":
        with pytest.raises(ApprovalRequired):
            await registry.get("environment-1", approved=True)


async def test_literature_fingerprint_is_rechecked(proof_case):
    hub, _, _ = proof_case
    citation = SourceLocation.model_validate(cite(hub))
    registry = ObjectRegistry(hub)
    with pytest.raises(EnvironmentUnavailable, match="Literature changed"):
        async with registry.verified_reads():
            await registry.source_evidence(citation)
            hub.literature[citation.literature_id]["source_sha256"] = "f" * 64
    assert registry._read_cache() is None


async def test_concurrent_operations_do_not_share_approval_cache(proof_case, monkeypatch):
    hub, _, _ = proof_case
    counts = count_artifacts(hub, monkeypatch)
    registry = ObjectRegistry(hub)
    entered, release = asyncio.Event(), asyncio.Event()
    async def first():
        async with registry.verified_reads():
            await registry.get("environment-1", approved=True)
            entered.set()
            await release.wait()
    pending = asyncio.create_task(first())
    await entered.wait()
    try:
        async with registry.verified_reads():
            await registry.get("environment-1", approved=True)
    finally:
        release.set()
        await pending
    assert counts["environment-1"] == 4


async def test_proof_metadata_reuses_inputs_but_retains_exact_target_checks(proof_case, monkeypatch):
    hub, task, artifact = proof_case
    counts = count_artifacts(hub, monkeypatch)
    report = await ProofVerifier().validate(hub, task, artifact)
    assert report.verdict == "accepted", report
    assert report.details["proof_accepted"]
    assert "exact-target" in report.details["checks"]
    assert counts["statement-1"] == 0
    assert counts["environment-1"] == 2


async def test_parallel_visits_in_one_scope_keep_one_input_view(proof_case, monkeypatch):
    hub, _, _ = proof_case
    calls = []
    original = hub.artifact
    async def delayed(key):
        calls.append(key)
        await asyncio.sleep(0)
        return await original(key)
    monkeypatch.setattr(hub, "artifact", delayed)
    registry = ObjectRegistry(hub)
    async with registry.verified_reads():
        await asyncio.gather(*(registry.get("environment-1", approved=True) for _ in range(5)))
    assert calls == ["environment-1", "environment-1"]


async def test_inherited_child_cannot_reuse_a_closed_approval_scope(proof_case):
    hub, _, _ = proof_case
    registry = ObjectRegistry(hub)
    release = asyncio.Event()
    async def child():
        await release.wait()
        with pytest.raises(ApprovalRequired):
            await registry.get("environment-1", approved=True)
    async with registry.verified_reads():
        await registry.get("environment-1", approved=True)
        pending = asyncio.create_task(child())
    hub.artifacts["environment-1"]["evidence"].append({"policy": "lean.environment@1", "verdict": "rejected"})
    release.set()
    await pending
