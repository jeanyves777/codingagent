from config_loader import DEBUG, load_config


def test_comments_blank_and_whitespace():
    text = "# comment\n\n  HOST = example.org  \nPORT=8080\n   \n#X=1\n"
    assert load_config(text) == {"HOST": "example.org", "PORT": "8080"}


def test_value_with_equals():
    assert load_config("URL = a=b") == {"URL": "a=b"}


def test_debug_still_off():
    assert DEBUG is False
