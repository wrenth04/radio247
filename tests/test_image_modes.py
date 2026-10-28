import pytest

from radio247.playout import show_default


@pytest.mark.parametrize(
    ("mode", "now", "expected"),
    [
        ("start", 5, True),
        ("start", 50, False),
        ("end", 95, True),
        ("end", 50, False),
        ("both", 5, True),
        ("both", 95, True),
        ("both", 50, False),
        ("middle", 50, True),
        ("middle", 5, False),
        ("interval", 5, True),
        ("interval", 30, False),
        ("interval", 62, True),
        ("none", 5, False),
        ("none", 95, False),
        ("unknown", 5, False),
    ],
)
def test_show_default(mode, now, expected):
    assert show_default(mode, now, total=100, window=10, period=60) is expected
