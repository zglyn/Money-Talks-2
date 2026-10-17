"""Keep unit tests offline: never call live PRISM or Nicrron from env leftovers."""

import pytest


@pytest.fixture(autouse=True)
def _offline_secrets(monkeypatch):
    for key in (
        "PRISMTRACE_API_KEY",
        "PRISMTRACE_PROJECT_ID",
        "PRISMTRACE_HOST",
        "NICRRON_API_KEY",
        "PRISMTRACE_EXPORTER",
    ):
        monkeypatch.delenv(key, raising=False)
    # Reset prism client cache so env deletions take effect per test module import order.
    import creditpool.prism as prism_mod

    monkeypatch.setattr(prism_mod, "_CLIENT", None)
    monkeypatch.setattr(prism_mod, "_CLIENT_TRIED", False)
