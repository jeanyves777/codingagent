from textstats import average_word_length, word_count


def test_whitespace_variants():
    assert word_count("  hello \t world\n\nagain ") == 3


def test_empty_and_blank():
    assert word_count("") == 0
    assert word_count("   \n") == 0


def test_average():
    assert average_word_length("ab abcd") == 3.0
    assert average_word_length("") == 0.0
    assert average_word_length(" \t ") == 0.0
