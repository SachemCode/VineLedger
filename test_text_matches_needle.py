"""Pending / bulk UI needle helper (_text_matches_needle)."""

import app


def test_empty_needle_matches_all():
    assert app._text_matches_needle("Anything", "") is True
    assert app._text_matches_needle("Anything", "   ") is True


def test_substring_case_insensitive():
    assert app._text_matches_needle("Hello World", "world") is True
    assert app._text_matches_needle("Hello World", "WORLD") is True
    assert app._text_matches_needle("Hello", "xyz") is False


def test_none_haystack():
    assert app._text_matches_needle(None, "a") is False
    assert app._text_matches_needle(None, "") is True
