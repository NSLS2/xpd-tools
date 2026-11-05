"""Tiled-access primitives shared by every evaluation plugin."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Hashable, Mapping, Sequence
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

_T = TypeVar("_T")


class _TiledAccessError(RuntimeError):
    """Mark an exception raised while looking up or reading Tiled data."""


def _read_stream_dataset(
    client: Any, uid: Hashable, stream_name: str
) -> tuple[Any, Mapping[str, Any]]:
    """Read a stream dataset from Tiled, with retries on failure."""
    try:
        run = client[uid]
        dataset = run[stream_name].read()
        metadata = run.metadata["start"]
    except Exception as exc:
        raise _TiledAccessError(
            f"failed to read stream {stream_name!r} for uid={uid!r}"
        ) from exc
    return dataset, metadata


def _retry_access(
    uid: Hashable,
    operation: str,
    read: Callable[[], _T | None],
    missing: Callable[[], Sequence[str]],
    *,
    max_retries: int,
    retry_delay: float,
) -> _T:
    """
    Retry `read` up to `max_retries` times with `retry_delay` between attempts.

    Args:
        - uid: Unique identifier for the run.
        - operation: Description of the operation being performed.
        - read: Callable that performs the read operation.
        - missing: Callable that returns a sequence of missing keys.
        - max_retries: Maximum number of retries.
        - retry_delay: Delay between retries in seconds.

    Returns
    -------
        - The result of the read operation.
    """
    last_access_error: BaseException | None = None
    for attempt in range(max_retries):
        try:
            result = read()
        except _TiledAccessError as exc:
            last_access_error = exc.__cause__ or exc
            logger.warning(
                "Failed to read %s for uid=%r (attempt %d/%d): %r",
                operation,
                uid,
                attempt + 1,
                max_retries,
                last_access_error,
            )
        else:
            if result is not None:
                return result
        if attempt + 1 < max_retries:
            time.sleep(retry_delay)

    missing_items = tuple(missing())
    suffix = f" Missing: {', '.join(missing_items)}." if missing_items else ""
    raise RuntimeError(
        f"Failed to read {operation} for uid={uid!r} after "
        f"{max_retries} attempts.{suffix}"
    ) from last_access_error
