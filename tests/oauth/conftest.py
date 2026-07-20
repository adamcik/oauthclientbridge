from dataclasses import dataclass

import pytest

from oauthclientbridge.oauth import (
    _core as oauth_core,  # pyright: ignore[reportPrivateUsage] # Direct implementation test fixture.
)


@dataclass
class MockTime:
    wall_seconds: float = 0.0
    monotonic_seconds: float = 0.0

    def time(self) -> float:
        return self.wall_seconds

    def monotonic(self) -> float:
        return self.monotonic_seconds

    def sleep(self, seconds: float) -> None:
        self.advance(monotonic=seconds)

    def advance(self, *, monotonic: float, wall: float | None = None) -> None:
        self.monotonic_seconds += monotonic
        self.wall_seconds += monotonic if wall is None else wall


@pytest.fixture
def mock_time(monkeypatch: pytest.MonkeyPatch) -> MockTime:
    clock = MockTime()
    monkeypatch.setattr(oauth_core.time, "time", clock.time)
    monkeypatch.setattr(oauth_core.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(oauth_core.time, "sleep", clock.sleep)
    return clock
