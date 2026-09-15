"""Bundled fake HOL Light runtime for harness tests.

This package deliberately emulates the operational transcript surface of
HOL Light on a narrow set of rails. It is not a theorem prover.
"""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .runtime import (
        FakeHolConfig,
        FakeHolResult,
        FakeHolRuntime,
        FakeHolScenario,
        FakeTheorem,
        generate_transcript,
        normalize_statement,
    )

__all__ = [
    "FakeHolConfig",
    "FakeHolResult",
    "FakeHolRuntime",
    "FakeHolScenario",
    "FakeTheorem",
    "generate_transcript",
    "normalize_statement",
]


def __getattr__(name: str) -> Any:
    if name in __all__:
        from . import runtime

        return getattr(runtime, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
