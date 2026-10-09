import pytest

from duration import parse_duration


def test_valid():
    assert parse_duration("1h30m") == 5400
    assert parse_duration("45m") == 2700
    assert parse_duration("2h") == 7200
    assert parse_duration("90s") == 90
    assert parse_duration("1h1m1s") == 3661


@pytest.mark.parametrize("text", ["abc", "", "5x", "h", "10"])
def test_invalid(text):
    with pytest.raises(ValueError):
        parse_duration(text)
