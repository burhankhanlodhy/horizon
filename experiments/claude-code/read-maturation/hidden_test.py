import pytest
from vendor import configparser, textwrap


def test_bool_words():
    p = configparser.ConfigParser()
    p.read_string("[s]\na = enabled\nb = Disabled\nc = yes\nd = maybe\n")
    assert p.getboolean("s", "a") is True and p.getboolean("s", "b") is False and p.getboolean("s", "c") is True
    with pytest.raises(ValueError):
        p.getboolean("s", "d")


def test_wrap_paragraphs_more():
    assert textwrap.wrap_paragraphs("a b c", width=80) == "a b c"
    assert textwrap.wrap_paragraphs("x\n\n\n\ny", width=5) == "x\n\ny"
