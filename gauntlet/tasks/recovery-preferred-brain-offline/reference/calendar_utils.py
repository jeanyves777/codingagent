"""Calendar helpers."""


def is_leap_year(year):
    """Return True for Gregorian leap years."""
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
