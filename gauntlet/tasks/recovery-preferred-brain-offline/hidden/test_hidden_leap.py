from calendar_utils import is_leap_year


def test_rules():
    assert is_leap_year(2024)
    assert not is_leap_year(2023)
    assert not is_leap_year(1900)
    assert is_leap_year(2000)
