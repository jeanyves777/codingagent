from tags import add_tag


def test_independent_calls():
    assert add_tag("a") == ["a"]
    assert add_tag("b") == ["b"]


def test_appends_to_given_list():
    existing = ["x"]
    result = add_tag("y", existing)
    assert result is existing and existing == ["x", "y"]
