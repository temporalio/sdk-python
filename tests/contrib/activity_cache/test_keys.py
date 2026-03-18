"""Tests for cache key computation."""

from dataclasses import dataclass

from temporalio.contrib.activity_cache._keys import compute_cache_key


class TestComputeCacheKey:
    """Tests for deterministic cache key generation."""

    def test_same_inputs_same_key(self) -> None:
        """Identical inputs produce identical keys."""
        key1 = compute_cache_key("fn", ("a", "b"))
        key2 = compute_cache_key("fn", ("a", "b"))
        assert key1 == key2

    def test_different_inputs_different_key(self) -> None:
        """Different inputs produce different keys."""
        key1 = compute_cache_key("fn", ("a", "b"))
        key2 = compute_cache_key("fn", ("a", "c"))
        assert key1 != key2

    def test_different_fn_name_different_key(self) -> None:
        """Different function names produce different keys."""
        key1 = compute_cache_key("fn_a", ("x",))
        key2 = compute_cache_key("fn_b", ("x",))
        assert key1 != key2

    def test_key_is_32_hex_chars(self) -> None:
        """Keys are 32-character hex strings."""
        key = compute_cache_key("fn", ("input",))
        assert len(key) == 32
        assert all(c in "0123456789abcdef" for c in key)

    def test_key_fn_selects_args(self) -> None:
        """key_fn controls which arguments are included."""
        key1 = compute_cache_key(
            "fn", ("a", "b"), key_fn=lambda x, y: {"x": x}
        )
        key2 = compute_cache_key(
            "fn", ("a", "DIFFERENT"), key_fn=lambda x, y: {"x": x}
        )
        assert key1 == key2

    def test_key_fn_different_selected_args(self) -> None:
        """key_fn with different selected values produces different keys."""
        key1 = compute_cache_key(
            "fn", ("a", "b"), key_fn=lambda x, y: {"x": x}
        )
        key2 = compute_cache_key(
            "fn", ("DIFFERENT", "b"), key_fn=lambda x, y: {"x": x}
        )
        assert key1 != key2

    def test_dataclass_inputs(self) -> None:
        """Dataclass inputs are serialized deterministically."""

        @dataclass
        class Input:
            name: str
            value: int

        key1 = compute_cache_key("fn", (Input("test", 42),))
        key2 = compute_cache_key("fn", (Input("test", 42),))
        key3 = compute_cache_key("fn", (Input("test", 99),))
        assert key1 == key2
        assert key1 != key3

    def test_bytes_content_addressed(self) -> None:
        """Bytes inputs use content hash, not identity."""
        key1 = compute_cache_key("fn", (b"hello",))
        key2 = compute_cache_key("fn", (b"hello",))
        key3 = compute_cache_key("fn", (b"world",))
        assert key1 == key2
        assert key1 != key3
