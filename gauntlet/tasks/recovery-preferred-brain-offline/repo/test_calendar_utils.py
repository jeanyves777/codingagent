from calendar_utils import is_leap_year


def test_century_rule():
    assert not is_leap_year(1900)
