from pagination import page_count, paginate


def test_second_page():
    assert paginate([1, 2, 3, 4, 5], 2, 2) == [3, 4]


def test_partial_last_page_counts():
    assert page_count(5, 2) == 3
