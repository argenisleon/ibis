"""Test utilities."""

import pickle

import ibis
import ibis.util as util


def assert_equal(left, right):
    """Assert that two ibis objects are equal."""

    if util.all_of([left, right], ibis.Schema):
        assert left.equals(right), 'Comparing schemas: \n{!r} !=\n{!r}'.format(
            left, right
        )
    else:
        assert left.equals(right), 'Objects unequal: \n{}\nvs\n{}'.format(
            repr(left), repr(right)
        )


def assert_pickle_roundtrip(obj):
    """Assert that an ibis object remains the same after pickling and
    unpickling."""
    loaded = pickle.loads(pickle.dumps(obj))
    if hasattr(obj, "equals"):
        assert obj.equals(loaded)
    else:
        assert obj == loaded
