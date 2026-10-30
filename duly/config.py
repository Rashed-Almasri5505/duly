"""Configuration and secrets.

Secrets are read from Streamlit secrets when deployed, then from environment variables
(a local .env file is loaded into the environment). Secret values are never logged or printed.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "config.yaml"
SKILLS_DIR = REPO_ROOT / "skills"
SAMPLES_DIR = REPO_ROOT / "samples"

load_dotenv(REPO_ROOT / ".env", override=False)

OFFLINE_ENV = "DULY_OFFLINE"   # set to 1 to behave like a deployment without API keys
API_KEYS = ("NEBIUS_API_KEY", "TAVILY_API_KEY")


@lru_cache(maxsize=1)
def load_config() -> dict[str, Any]:
    with CONFIG_PATH.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def _streamlit_secret(name: str) -> str | None:
    try:
        import streamlit as st
        from streamlit import runtime

        if runtime.exists() and name in st.secrets:
            return str(st.secrets[name])
    except Exception:
        # No Streamlit runtime or no secrets.toml: fall back to the environment.
        pass
    return None


def get_secret(name: str) -> str | None:
    """Return a secret by name, or None if it is unset or empty."""
    if name in API_KEYS and os.environ.get(OFFLINE_ENV):
        return None
    value = _streamlit_secret(name) or os.environ.get(name)
    return value.strip() if value and value.strip() else None


def demo_mode() -> bool:
    """True on the hosted demo, where every visitor gets a temporary copy of the sample data."""
    return (get_secret("DULY_DEMO_MODE") or "").lower() in ("1", "true", "yes")


def default_data_dir() -> Path:
    """Where a local install keeps the user's documents, memory and placeholder vault."""
    return Path(get_secret("DULY_DATA_DIR") or REPO_ROOT / "data" / "user")
