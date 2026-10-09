from config_loader import DEBUG, load_config


def test_simple():
    assert load_config("A=1") == {"A": "1"}


def test_debug_off():
    assert DEBUG is False
