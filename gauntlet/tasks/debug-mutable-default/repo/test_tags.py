from tags import add_tag


def test_calls_without_a_list_do_not_share_state():
    first = add_tag("red")
    second = add_tag("blue")
    assert first == ["red"] and second == ["blue"]
