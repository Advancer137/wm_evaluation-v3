"""Builders for complete answer sheets of the current question set (used by the tests)."""

DEFAULT_SIDE = {"validity": "valid", "base_task": "yes", "retention": "pass", "retention_reasons": None,
                "retention_note": None, "preference_a": "yes", "preference_b": "no"}


def side_answers(side, **changes):
    """One video's answers (side "x" or "y"); defaults: valid, task done, conditions kept, A yes / B no."""
    values = dict(DEFAULT_SIDE, **changes)
    return {f"{side}_{key}": value for key, value in values.items()}


def sheet(x=None, y=None):
    """A complete sheet; ``x`` / ``y`` are keyword changes for that video."""
    return {**side_answers("x", **(x or {})), **side_answers("y", **(y or {}))}
