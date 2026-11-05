from __future__ import annotations

from unittest.mock import call, patch

import pytest

from xpd_tools.optimization.helpers.common import _retry_access, _TiledAccessError


def test_retry_access_sleeps_the_configured_delay_between_attempts() -> None:
    """time.sleep must actually be called with the configured retry_delay --
    not a hardcoded value, and not dropped entirely.
    """
    attempts = 0

    def read() -> str:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise _TiledAccessError("not ready")
        return "ok"

    with patch("xpd_tools.optimization.helpers.common.time.sleep") as sleep:
        result = _retry_access(
            "uid", "widget", read, lambda: (), max_retries=5, retry_delay=7.5
        )

    assert result == "ok"
    assert attempts == 3
    # Two failed attempts before the third succeeds -> exactly two sleeps,
    # each for the configured delay.
    assert sleep.call_args_list == [call(7.5), call(7.5)]


def test_retry_access_does_not_sleep_after_the_final_attempt() -> None:
    def read() -> None:
        raise _TiledAccessError("never ready")

    with patch("xpd_tools.optimization.helpers.common.time.sleep") as sleep:
        with pytest.raises(RuntimeError, match="after 3 attempts"):
            _retry_access(
                "uid", "widget", read, lambda: (), max_retries=3, retry_delay=2.0
            )

    # Sleeps only *between* attempts, never after the last one before raising.
    assert sleep.call_args_list == [call(2.0), call(2.0)]


def test_retry_access_never_sleeps_when_max_retries_is_one() -> None:
    def read() -> None:
        raise _TiledAccessError("never ready")

    with patch("xpd_tools.optimization.helpers.common.time.sleep") as sleep:
        with pytest.raises(RuntimeError, match="after 1 attempts"):
            _retry_access(
                "uid", "widget", read, lambda: (), max_retries=1, retry_delay=2.0
            )

    sleep.assert_not_called()
