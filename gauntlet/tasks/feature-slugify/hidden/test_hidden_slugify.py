from text_utils import shout, slugify


def test_slugify():
    assert slugify("Hello, World!") == "hello-world"
    assert slugify("  Café   Crème  ") == "cafe-creme"
    assert slugify("a--b__c") == "a-b-c"
    assert slugify("2024 Report (final)") == "2024-report-final"
    assert slugify("!!!") == ""


def test_existing_function_unchanged():
    assert shout("hi") == "HI!"
