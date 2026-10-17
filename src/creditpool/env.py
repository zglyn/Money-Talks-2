"""Load local `.env` into process env. Never invent or hard-code secrets."""

from pathlib import Path

_LOADED = False


def load(dotenv_path: str | Path | None = None) -> None:
    """Load `.env` from the repo root (or `dotenv_path`) once. Missing file is fine."""
    global _LOADED
    if _LOADED:
        return
    try:
        from dotenv import load_dotenv
    except ImportError:
        _LOADED = True
        return
    path = Path(dotenv_path) if dotenv_path else Path.cwd() / ".env"
    load_dotenv(path, override=False)
    _LOADED = True
