"""The ACP agent's own memory leaked across runs and poisoned cases permanently."""
from __future__ import annotations

from pathlib import Path

import pytest

from bench.cleanup import acp_memory_dir, reset_agent_memory


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    project = tmp_path / "dev" / "condor"
    project.mkdir(parents=True)
    memory = acp_memory_dir(project)
    memory.mkdir(parents=True)
    return project, memory


def test_memory_dir_matches_how_claude_code_derives_it(tmp_path, monkeypatch):
    """Path separators flatten to dashes, so /home/x/dev/condor keys -home-x-dev-condor.

    Both bench (`bench.client`) and production condor
    (`condor.runtime.llm_client.get_project_dir`) launch the bridge in the condor
    project root, so both land on this directory. That is what makes clearing it a
    cold-start *simulation* of production rather than a divergence from it.
    """
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    got = acp_memory_dir("/home/x/dev/condor")
    assert got == tmp_path / "cfg" / "projects" / "-home-x-dev-condor" / "memory"


def test_a_note_from_a_previous_run_is_archived_then_cleared(fake_home):
    """`agent_directional_trader_008` could never pass again once this file existed.

    Asked to remember a chop filter, the agent wrote `feedback_chop_filter.md` here
    instead of calling `manage_memory`. On the next run it read that file back, said
    "already saved, no changes needed", made no tool calls at all, and failed the
    `manage_memory` post-condition — correctly, since condor's store was still empty.
    Declining to duplicate an existing note is the right behaviour, which is what
    made the failure permanent and self-reinforcing.
    """
    project, memory = fake_home
    (memory / "feedback_chop_filter.md").write_text("do not enter in chop")
    (memory / "MEMORY.md").write_text("- [Chop filter](feedback_chop_filter.md)")
    archive = Path(project).parent / "archive"

    reset = reset_agent_memory(project, archive)

    assert reset.ok
    assert sorted(reset.archived) == ["MEMORY.md", "feedback_chop_filter.md"]
    assert list(memory.iterdir()) == [], "the next run must not see these"
    assert (archive / "feedback_chop_filter.md").read_text() == "do not enter in chop"


def test_files_are_archived_not_destroyed(fake_home):
    """The directory is the user's Claude Code install, not bench scratch.

    Today every file in it is a bench artifact, but anyone who uses Claude Code on
    the condor checkout would keep real notes here, and a benchmark has no business
    destroying them. The archive also records what state a run started from.
    """
    project, memory = fake_home
    (memory / "handwritten.md").write_text("a real note someone wrote")
    archive = Path(project).parent / "archive"

    reset = reset_agent_memory(project, archive)

    assert (archive / "handwritten.md").read_text() == "a real note someone wrote"
    assert reset.archive_dir == str(archive)


def test_reset_is_idempotent_and_silent_when_there_is_nothing_to_do(fake_home):
    project, _ = fake_home
    archive = Path(project).parent / "archive"
    first = reset_agent_memory(project, archive)
    assert first.ok and first.archived == []
    assert reset_agent_memory(project, archive).archived == []


def test_a_missing_directory_is_not_an_error(tmp_path, monkeypatch):
    """A first run, or a PydanticAI-only machine, has no such directory."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    reset = reset_agent_memory(tmp_path / "nope", tmp_path / "archive")
    assert reset.ok and reset.archived == []


def test_housekeeping_never_raises(fake_home, monkeypatch):
    """A cleanup failure must not turn a whole run into an error."""
    project, memory = fake_home
    (memory / "note.md").write_text("x")

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr("bench.cleanup.shutil.copy2", boom)
    reset = reset_agent_memory(project, Path(project).parent / "archive")
    assert not reset.ok and "disk full" in reset.error
    assert (memory / "note.md").exists(), "a failed archive must not delete anything"


def test_only_acp_models_have_memory_to_clear():
    """The API path is stateless, so there is nothing to reset and nothing to skew.

    This is the asymmetry the reset exists to close: an API model's only persistent
    memory is condor's own `manage_memory` store, while Claude Code has that *plus*
    a private one condor cannot read.
    """
    from condor_compat.acp.acp_client import is_acp_model

    assert is_acp_model("claude-code")
    assert not is_acp_model("anthropic:claude-sonnet-5")
    assert not is_acp_model("ollama:qwen2.5:14b")
    assert not is_acp_model("openrouter:anthropic/claude-haiku-4-5")
