"""Shared pytest setup."""

import pytest

from coinjoin_pipeline import registry


@pytest.fixture(autouse=True)
def offline_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep tests off the network: every tag stays unpinned unless a test supplies digests."""

    def unreachable(reference: str, timeout: float = 0) -> str:
        raise registry.RegistryError(f"registry lookups are disabled in tests: {reference}")

    monkeypatch.setattr(registry, "registry_digest", unreachable)
    registry.pinned_reference.cache_clear()
