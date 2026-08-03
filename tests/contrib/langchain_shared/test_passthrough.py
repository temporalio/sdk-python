"""The shared sandbox passthrough-list merge."""

from __future__ import annotations

from temporalio.contrib._langchain._passthrough import merge_passthrough_modules


def test_merge_dedupes_preserving_first_occurrence_order() -> None:
    merged = merge_passthrough_modules(("a", "b", "c"), ["b", "d", "a", "e"])
    assert merged == ("a", "b", "c", "d", "e")


def test_merge_tolerates_no_user_list() -> None:
    assert merge_passthrough_modules(("a", "b"), None) == ("a", "b")
    assert merge_passthrough_modules((), None) == ()
