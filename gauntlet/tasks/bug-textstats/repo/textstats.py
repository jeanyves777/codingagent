"""Small text statistics helpers."""


def word_count(text):
    """Return the number of words in text."""
    return len(text.split(" "))


def average_word_length(text):
    """Return the mean word length, or 0.0 for text without words."""
    words = text.split()
    return sum(len(word) for word in words) / len(words)
