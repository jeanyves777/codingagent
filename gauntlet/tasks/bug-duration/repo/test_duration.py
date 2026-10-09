import pytest

from duration import parse_duration


def test_minutes_are_sixty_seconds():
    assert parse_duration("2m") == 120


def test_not_a_duration():
    with pytest.raises(ValueError):
        parse_duration("xyz")
