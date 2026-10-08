from textstats import word_count


def test_word_count_simple():
    assert word_count("hello world") == 2
