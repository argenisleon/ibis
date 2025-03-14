import numpy as np
import pytest

import ibis
import ibis.expr.datatypes as dt

pytestmark = [
    pytest.mark.never(["sqlite", "mysql"], reason="No map support"),
    pytest.mark.notimpl(
        ["duckdb", "postgres", "impala", "datafusion", "pyspark", "snowflake"],
        reason="Not implemented yet",
    ),
]


def test_literal_map_keys(con):
    mapping = ibis.literal({'1': 'a', '2': 'b'})
    expr = mapping.keys().name('tmp')

    result = con.execute(expr)
    # This does not check whether `result` is an np.array or a list,
    # because it varies across backends and backend configurations
    assert np.array_equal(result, ['1', '2'])


@pytest.mark.notimpl(
    ["snowflake"],
    reason="snowflake doesn't implement map values",
)
def test_literal_map_values(con):
    mapping = ibis.literal({'1': 'a', '2': 'b'})
    expr = mapping.values().name('tmp')

    result = con.execute(expr)
    assert np.array_equal(result, ['a', 'b'])


def test_scalar_isin_literal_map_keys(con):
    mapping = ibis.literal({'a': 1, 'b': 2})
    a = ibis.literal('a')
    c = ibis.literal('c')
    true = a.isin(mapping.keys())
    false = c.isin(mapping.keys())
    assert con.execute(true) == True  # noqa: E712
    assert con.execute(false) == False  # noqa: E712


def test_map_scalar_contains_key_scalar(con):
    mapping = ibis.literal({'a': 1, 'b': 2})
    a = ibis.literal('a')
    c = ibis.literal('c')
    true = mapping.contains(a)
    false = mapping.contains(c)
    assert con.execute(true) == True  # noqa: E712
    assert con.execute(false) == False  # noqa: E712


def test_map_scalar_contains_key_column(backend, alltypes, df):
    value = {'1': 'a', '3': 'c'}
    mapping = ibis.literal(value)
    expr = mapping.contains(alltypes.string_col).name('tmp')
    result = expr.execute()
    expected = df.string_col.apply(lambda x: x in value).rename('tmp')
    backend.assert_series_equal(result, expected)


def test_map_column_contains_key_scalar(backend, alltypes, df):
    expr = ibis.map(
        ibis.array([alltypes.string_col]), ibis.array([alltypes.int_col])
    )
    series = df.apply(lambda row: {row['string_col']: row['int_col']}, axis=1)

    result = expr.contains('1').name('tmp').execute()
    series = series.apply(lambda x: '1' in x).rename('tmp')

    backend.assert_series_equal(result, series)


def test_map_column_contains_key_column(backend, alltypes, df):
    expr = ibis.map(
        ibis.array([alltypes.string_col]), ibis.array([alltypes.int_col])
    )
    result = expr.contains(alltypes.string_col).name('tmp').execute()
    assert result.all()


def test_literal_map_merge(con):
    a = ibis.literal({'a': 0, 'b': 2})
    b = ibis.literal({'a': 1, 'c': 3})
    expr = a + b

    assert con.execute(expr) == {'a': 1, 'b': 2, 'c': 3}


def test_literal_map_getitem_broadcast(backend, alltypes, df):
    value = {'1': 'a', '2': 'b'}

    lookup_table = ibis.literal(value)
    expr = lookup_table[alltypes.string_col]

    result = expr.name('tmp').execute()
    expected = df.string_col.apply(lambda x: value.get(x, None)).rename('tmp')

    backend.assert_series_equal(result, expected)


def test_literal_map_get_broadcast(backend, alltypes, df):
    value = {'1': 'a', '2': 'b'}

    lookup_table = ibis.literal(value)
    expr = lookup_table.get(alltypes.string_col, 'default')

    result = expr.name('tmp').execute()
    expected = df.string_col.apply(lambda x: value.get(x, 'default')).rename(
        'tmp'
    )

    backend.assert_series_equal(result, expected)


def test_map_construction(backend, con, alltypes, df):
    expr = ibis.map(['a', 'b'], [1, 2])
    result = con.execute(expr.name('tmp'))
    assert result == {'a': 1, 'b': 2}

    expr = ibis.map(
        ibis.array([alltypes.string_col]), ibis.array([alltypes.int_col])
    )
    result = con.execute(expr)
    expected = df.apply(
        lambda row: {row['string_col']: row['int_col']}, axis=1
    )

    assert result.to_list() == expected.to_list()


def test_map_get_with_compatible_value_smaller(con):
    value = ibis.literal({'A': 1000, 'B': 2000})
    expr = value.get('C', 3)
    assert con.execute(expr) == 3


def test_map_get_with_compatible_value_bigger(con):
    value = ibis.literal({'A': 1, 'B': 2})
    expr = value.get('C', 3000)
    assert con.execute(expr) == 3000


def test_map_get_with_incompatible_value_different_kind(con):
    value = ibis.literal({'A': 1000, 'B': 2000})
    expr = value.get('C', 3.0)
    assert con.execute(expr) == 3.0


@pytest.mark.parametrize('null_value', [None, ibis.NA])
def test_map_get_with_null_on_not_nullable(con, null_value):
    map_type = dt.Map(dt.string, dt.Int16(nullable=False))
    value = ibis.literal({'A': 1000, 'B': 2000}).cast(map_type)
    expr = value.get('C', null_value)
    assert con.execute(expr) is None


@pytest.mark.parametrize('null_value', [None, ibis.NA])
def test_map_get_with_null_on_null_type_with_null(con, null_value):
    value = ibis.literal({'A': None, 'B': None})
    expr = value.get('C', null_value)
    assert con.execute(expr) is None


def test_map_get_with_null_on_null_type_with_non_null(con):
    value = ibis.literal({'A': None, 'B': None})
    expr = value.get('C', 1)
    assert con.execute(expr) == 1
