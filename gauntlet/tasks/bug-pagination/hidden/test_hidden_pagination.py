from pagination import page_count, paginate

ITEMS = list(range(10))


def test_first_and_last_page():
    assert paginate(ITEMS, 1, 3) == [0, 1, 2]
    assert paginate(ITEMS, 4, 3) == [9]
    assert paginate(ITEMS, 5, 3) == []


def test_page_count():
    assert page_count(10, 3) == 4
    assert page_count(9, 3) == 3
    assert page_count(0, 3) == 0
