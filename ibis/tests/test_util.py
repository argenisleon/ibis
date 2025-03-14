"""Test ibis.util utilities."""


import pytest

from ibis import util


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ([], []),
        ([1], [1]),
        ([1, 2], [1, 2]),
        ([[]], []),
        ([[1]], [1]),
        ([[1, 2]], [1, 2]),
        ([[1, [2, [3, [4]]]]], [1, 2, 3, 4]),
        ([[4, [3, [2, [1]]]]], [4, 3, 2, 1]),
        ([[[[4], 3], 2], 1], [4, 3, 2, 1]),
        ([[[[1], 2], 3], 4], [1, 2, 3, 4]),
        ([{(1,), frozenset({(2,)})}], {1, 2}),
        ({(1, (2,)): None, "a": None}, [1, 2, "a"]),
        (([x] for x in range(5)), list(range(5))),
        ({(1, (2, frozenset({(3,)})))}, [1, 2, 3]),
    ],
)
def test_flatten(case, expected):
    assert type(expected)(util.flatten_iterable(case)) == expected


@pytest.mark.parametrize("case", [1, "abc", b"abc", 2.0, object()])
def test_flatten_invalid_input(case):
    flat = util.flatten_iterable(case)

    with pytest.raises(TypeError):
        list(flat)


def test_dotdict():
    d = util.DotDict({"a": 1, "b": 2, "c": 3})
    assert d["a"] == d.a == 1
    assert d["b"] == d.b == 2

    d.b = 3
    assert d.b == 3
    assert d["b"] == 3

    del d.c
    assert not hasattr(d, "c")
    assert "c" not in d

    assert repr(d) == "DotDict({'a': 1, 'b': 3})"

    with pytest.raises(KeyError):
        assert d['x']
    with pytest.raises(AttributeError):
        assert d.x
