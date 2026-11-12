"""Tiled-access primitives shared by every evaluation plugin."""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable, Hashable, Mapping, Sequence
from typing import Any, TypeVar

import numpy as np
from numpy.typing import ArrayLike, NDArray

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

    Returns:
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


def pdfstream_str_to_1d(x: ArrayLike | str | bytes | np.str_ | np.bytes_) -> NDArray[np.float64]:
    """Convert numeric or string-like containers to a 1D float64 array.

    This handles cases where tiled (or intermediate serializers) return values
    as:
    - plain strings / numpy string scalars,
    - 0-D arrays holding one string,
    - 1-D object/string arrays,
    - ordinary numeric array-likes.
    """

    def _parse_text(text: str) -> NDArray[np.float64]:
        s = text.strip()
        if not s:
            return np.array([], dtype=float)
        s = s.strip("[]")

        # Fast path: whitespace-delimited numeric text.
        try:
            arr = np.fromstring(s, sep=" ", dtype=float)
            if arr.size > 0:
                return arr
        except ValueError:
            pass

        # Robust fallback: extract numeric literals from mixed wrapper text.
        vals = re.findall(
            r"[-+]?(?:\d*\.\d+|\d+\.?)(?:[eE][-+]?\d+)?|(?:nan|inf|-inf)",
            s,
            flags=re.IGNORECASE,
        )
        return np.array([float(v) for v in vals], dtype=float)

    if isinstance(x, (str, np.str_)):
        return _parse_text(str(x))
    if isinstance(x, (bytes, np.bytes_)):
        return _parse_text(bytes(x).decode(errors="ignore"))

    arr = np.asarray(x)

    # Numeric fast path.
    if arr.dtype.kind in {"f", "i", "u", "b"}:
        return arr.astype(float).ravel()

    # Scalar container (often a 0-D array with one string/object).
    if arr.ndim == 0:
        return _parse_text(str(arr.item()))

    # String/object arrays: try direct cast, then text parse fallback.
    if arr.dtype.kind in {"U", "S", "O"}:
        try:
            return arr.astype(float).ravel()
        except (TypeError, ValueError):
            joined = " ".join(map(str, arr.ravel()))
            return _parse_text(joined)

    # Last resort.
    return np.asarray(x, dtype=float).ravel()
