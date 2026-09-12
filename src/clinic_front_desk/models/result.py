"""Shared ``Result`` union and the ``StoreError`` / ``ToolError`` types.

Every Data_Layer write returns a ``Result[T, StoreError]`` and leaves prior
records unchanged on failure (Req 16.6). Every Strands tool returns a
``Result[T, ToolError]`` so the agent can branch on the failure ``kind``
(design "Strands Tool Suite").

The union is modelled as two small frozen dataclasses, :class:`Ok` and
:class:`Err`, that mirror the design's discriminated shape
``{ ok: true; value }`` | ``{ ok: false; error }``. ``ok`` is a class-level
constant (not an ``__init__`` argument) so both ``result.ok`` checks and
``isinstance`` narrowing work.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar, Generic, Literal, NoReturn, TypeAlias, TypeVar, Union

if sys.version_info >= (3, 13):  # pragma: no cover - version-dependent import
    from typing import TypeIs
else:  # pragma: no cover - version-dependent import
    from typing_extensions import TypeIs

T = TypeVar("T")
E = TypeVar("E")


@dataclass(frozen=True)
class Ok(Generic[T]):
    """Successful result carrying a ``value``."""

    value: T
    ok: ClassVar[Literal[True]] = True

    def unwrap(self) -> T:
        """Return the wrapped value."""
        return self.value


@dataclass(frozen=True)
class Err(Generic[E]):
    """Failed result carrying an ``error`` (a ``StoreError`` or ``ToolError``)."""

    error: E
    ok: ClassVar[Literal[False]] = False

    def unwrap(self) -> NoReturn:
        """Raise: an ``Err`` has no value to unwrap."""
        raise ValueError(f"called unwrap() on an Err: {self.error!r}")


# A Result is either an Ok[T] or an Err[E]. E defaults to StoreError at the
# store boundary and ToolError at the tool boundary; both are provided as
# convenience aliases below.
Result: TypeAlias = Union[Ok[T], Err[E]]


def is_ok(result: Result[T, E]) -> TypeIs[Ok[T]]:
    """Narrow a ``Result`` to :class:`Ok`.

    Uses ``TypeIs`` (PEP 742) rather than ``TypeGuard`` so narrowing applies in
    *both* branches: after an early ``if is_err(r): return ...`` guard, ``r`` is
    an :class:`Ok` for the rest of the function and ``r.value`` type-checks.
    """
    return result.ok


def is_err(result: Result[T, E]) -> TypeIs[Err[E]]:
    """Narrow a ``Result`` to :class:`Err` (bidirectional, see :func:`is_ok`)."""
    return not result.ok


# ---------------------------------------------------------------------------
# StoreError — the Data_Layer failure type (design "Data_Layer Interfaces").
# ---------------------------------------------------------------------------


class StoreErrorKind(StrEnum):
    """Discriminator for :class:`StoreError`.

    - ``store_failure`` — a read/write against the backing store failed.
    - ``not_found`` — a requested record does not exist.
    - ``validation`` — the write violated a store invariant, e.g. an
      Appointment/Slot/schedule write with a missing ``provider_id`` (Req 16.7).
    """

    STORE_FAILURE = "store_failure"
    NOT_FOUND = "not_found"
    VALIDATION = "validation"


@dataclass(frozen=True)
class StoreError:
    """A Data_Layer failure returned inside an :class:`Err`."""

    kind: StoreErrorKind
    detail: str
    store: str | None = None
    field: str | None = None


# ---------------------------------------------------------------------------
# ToolError — the Strands tool failure type (design "Strands Tool Suite").
# Modelled as a discriminated union of one frozen dataclass per ``kind``.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StoreFailure:
    """A persistence/read failure surfaced from a tool."""

    store: str
    detail: str
    kind: Literal["store_failure"] = "store_failure"


@dataclass(frozen=True)
class NotFound:
    """The requested record could not be found."""

    detail: str
    kind: Literal["not_found"] = "not_found"


@dataclass(frozen=True)
class Ambiguous:
    """More than one candidate matched; disambiguation is required (Req 3.6)."""

    candidates: list[str]
    kind: Literal["ambiguous"] = "ambiguous"


@dataclass(frozen=True)
class Validation:
    """An input field failed validation."""

    field: str
    detail: str
    kind: Literal["validation"] = "validation"


@dataclass(frozen=True)
class NotOffered:
    """The named service is not offered by the clinic (Req 2.9)."""

    named_service: str
    kind: Literal["not_offered"] = "not_offered"


@dataclass(frozen=True)
class Duplicate:
    """An active waitlist entry already exists for this patient/service (Req 7.5)."""

    entry_id: str
    kind: Literal["duplicate"] = "duplicate"


ToolError: TypeAlias = Union[
    StoreFailure,
    NotFound,
    Ambiguous,
    Validation,
    NotOffered,
    Duplicate,
]

# Convenience aliases matching the design's ``Result<T>`` (store-typed) and the
# tool-suite's ``Result`` (tool-typed).
StoreResult: TypeAlias = Result[T, StoreError]
ToolResult: TypeAlias = Result[T, ToolError]


__all__ = [
    "Ok",
    "Err",
    "Result",
    "StoreResult",
    "ToolResult",
    "is_ok",
    "is_err",
    "StoreError",
    "StoreErrorKind",
    "ToolError",
    "StoreFailure",
    "NotFound",
    "Ambiguous",
    "Validation",
    "NotOffered",
    "Duplicate",
]
