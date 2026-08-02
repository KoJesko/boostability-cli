"""Paths, model selection, and credential resolution."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

# Haiku 4.5 is the smallest model that supports both structured outputs and PDF
# input. Note it is a 200K-context model, which caps PDF documents at 100 pages
# (not the 600 that 1M-context models allow), and it uses the older
# {"type": "enabled", "budget_tokens": N} thinking API rather than adaptive
# thinking -- passing `effort` to this model is a 400.
MODEL = "claude-haiku-4-5"

MAX_TOKENS = 8000
THINKING_BUDGET = 2000  # only used with --think; must stay < MAX_TOKENS

# The Messages API rejects requests over 32 MB. Base64 inflates bytes by 4/3, so
# the real ceiling for an attachment is ~24 MB of PDF.
MAX_PDF_BYTES = 24 * 1024 * 1024


def data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
    d = Path(base) / "boostability"
    d.mkdir(parents=True, exist_ok=True)
    return d


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    d = Path(base) / "boostability"
    d.mkdir(parents=True, exist_ok=True)
    return d


def store_path() -> Path:
    return data_dir() / "store.json"


def config_path() -> Path:
    return config_dir() / "config.toml"


class MissingCredentials(Exception):
    """Raised when no usable Anthropic API key can be found."""


def api_key() -> str:
    """Resolve an API key: ANTHROPIC_API_KEY, then config.toml.

    Deliberately does NOT fall back to ~/.claude/.credentials.json -- that file
    holds a Claude Code OAuth token, which authenticates the Claude Code product
    and is not accepted by the Anthropic API.
    """
    env = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if env:
        return env

    path = config_path()
    if path.exists():
        try:
            key = tomllib.loads(path.read_text()).get("api_key", "").strip()
        except (tomllib.TOMLDecodeError, OSError) as exc:
            raise MissingCredentials(f"Could not read {path}: {exc}") from exc
        if key:
            return key

    raise MissingCredentials(
        "No Anthropic API key found.\n\n"
        "  Set it for this shell:\n"
        "    export ANTHROPIC_API_KEY='sk-ant-...'\n\n"
        f"  Or store it permanently in {path}:\n"
        '    api_key = "sk-ant-..."\n\n'
        "  Create a key at https://console.anthropic.com/settings/keys\n"
        "  (A Claude Code / claude.ai login is a different credential and will "
        "not work here.)"
    )
