"""Public knowledge is directly readable without per-worker reading copies."""
import json
from pathlib import Path

from formal_geyser.coordination.wiki import KNOWLEDGE_README, ensure_guides
from tests.coordination.test_native_workflow import native_case as native_case


async def test_large_notes_have_one_readme_entry_and_no_reading_copies(native_case):
    body = b"LongPublicNoteSentinel\n" * 20000
    case = await native_case(repository=True, files={KNOWLEDGE_README: b"# Public knowledge\n", "notes/long.md": body})
    context = await case.hub.prepare_native_context(case.author["worker_id"])
    assert "LongPublicNoteSentinel" not in json.dumps(context)
    assert Path(context["directories"]["knowledge_readme"]).read_text(encoding="utf-8") == "# Public knowledge\n"
    assert (Path(context["directories"]["public_notes"]) / "long.md").read_bytes() == body
    assert not (case.hub.workspace(case.author["worker_id"]) / ".private/wiki-versions").exists()
    assert context == await case.hub.prepare_native_context(case.author["worker_id"])


def test_starter_readme_distinguishes_required_guidance_and_reference_notes(tmp_path):
    guides = ensure_guides(tmp_path)
    readme = guides[KNOWLEDGE_README]
    body = readme.read_text(encoding="utf-8")
    assert "## Essential" in body and "## Reference notes" in body
    assert "literature.md" in body and "library.md" in body
    assert not (tmp_path / "wiki/index.md").exists()
    readme.write_text("# Authored project guidance\n", encoding="utf-8")
    assert ensure_guides(tmp_path)[KNOWLEDGE_README].read_text(encoding="utf-8") == "# Authored project guidance\n"
