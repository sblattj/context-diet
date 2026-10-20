"""Shared token estimate: ~chars/4, the same rule measure/start_tokens.py uses for logged blocks."""


def est_tokens(text):
    """Rough token count for text (chars / 4, rounded up; 0 for empty)."""
    return (len(text) + 3) // 4
