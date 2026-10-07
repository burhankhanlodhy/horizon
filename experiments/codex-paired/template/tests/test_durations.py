import pytest

from inventory.durations import parse_duration


@pytest.mark.parametrize("text,seconds", [("45s", 45), ("1h30m", 5400), ("2d", 172800), (" 1h30m15s ", 5415), ("1d2h", 93600)])
def test_parse(text, seconds):
    assert parse_duration(text) == seconds


@pytest.mark.parametrize("bad", ["", "abc", "10", "5x", "1m1h"])
def test_rejects(bad):
    with pytest.raises(ValueError):
        parse_duration(bad)
