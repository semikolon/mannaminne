"""The mail judge spends from mannaminne's own OpenRouter key, the shared one as fallback.

Fredrik, 2026-10-18: one key per project, so the dashboard can say who spent what.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "py"))
import mail_judge  # noqa: E402


def test_own_key_from_the_tier_file_wins(tmp_path, monkeypatch):
    (tmp_path / "tier-mac.env").write_text("export OPENROUTER_API_KEY=shared\nexport MANNAMINNE_OPENROUTER_API_KEY=own\n")
    monkeypatch.setattr(mail_judge, "_TIER_DIR", tmp_path)
    monkeypatch.delenv("MANNAMINNE_OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    mail_judge._load_key()
    assert os.environ["OPENROUTER_API_KEY"] == "own"


def test_own_key_from_env_beats_everything(tmp_path, monkeypatch):
    (tmp_path / "tier-mac.env").write_text("export OPENROUTER_API_KEY=shared\n")
    monkeypatch.setattr(mail_judge, "_TIER_DIR", tmp_path)
    monkeypatch.setenv("MANNAMINNE_OPENROUTER_API_KEY", "own-env")
    monkeypatch.setenv("OPENROUTER_API_KEY", "shared-env")
    mail_judge._load_key()
    assert os.environ["OPENROUTER_API_KEY"] == "own-env"


def test_shared_key_when_mannaminne_has_none(tmp_path, monkeypatch):
    (tmp_path / "tier-all.env").write_text("OPENROUTER_API_KEY=shared-all\n")
    monkeypatch.setattr(mail_judge, "_TIER_DIR", tmp_path)
    monkeypatch.delenv("MANNAMINNE_OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    mail_judge._load_key()
    assert os.environ["OPENROUTER_API_KEY"] == "shared-all"
