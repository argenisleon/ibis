import decimal

import numpy as np
import pandas as pd
import pandas.testing as tm
import pytest
from pytest import param

import ibis
import ibis.expr.datatypes as dt  # noqa: E402
from ibis.backends.pandas.execution import execute

TIMESTAMP = "2022-03-13 06:59:10.467417"


@pytest.mark.parametrize('from_', ['plain_float64', 'plain_int64'])
@pytest.mark.parametrize(
    ('to', 'expected'),
    [
        ('float16', 'float16'),
        ('float32', 'float32'),
        ('float64', 'float64'),
        ('float', 'float64'),
        ('int8', 'int8'),
        ('int16', 'int16'),
        ('int32', 'int32'),
        ('int64', 'int64'),
        ('string', 'object'),
    ],
)
def test_cast_numeric(t, df, from_, to, expected):
    c = t[from_].cast(to)
    result = c.execute()
    assert str(result.dtype) == expected


@pytest.mark.parametrize('from_', ['float64_as_strings', 'int64_as_strings'])
@pytest.mark.parametrize(
    ('to', 'expected'), [('double', 'float64'), ('string', 'object')]
)
def test_cast_string(t, df, from_, to, expected):
    c = t[from_].cast(to)
    result = c.execute()
    assert str(result.dtype) == expected


@pytest.mark.parametrize('from_', ['array_of_int64', 'array_of_float64'])
@pytest.mark.parametrize(
    ('to', 'expected'),
    [('array<double>', 'float64'), ('array<int32>', 'int32')],
)
def test_cast_array(t, df, from_, to, expected):
    c = t[from_].cast(to)
    result = c.execute()
    # The Series of arrays
    assert str(result.dtype) == 'object'
    # One of the arrays in the Series
    assert isinstance(result[0], np.ndarray)
    assert str(result[0].dtype) == expected


@pytest.mark.parametrize(
    ('to', 'expected'),
    [
        ('string', 'object'),
        ('int64', 'int64'),
        param('double', 'float64', marks=pytest.mark.xfail(raises=TypeError)),
        (
            dt.Timestamp('America/Los_Angeles'),
            'datetime64[ns, America/Los_Angeles]',
        ),
        (
            "timestamp('America/Los_Angeles')",
            'datetime64[ns, America/Los_Angeles]',
        ),
    ],
)
@pytest.mark.parametrize(
    'column',
    ['plain_datetimes_naive', 'plain_datetimes_ny', 'plain_datetimes_utc'],
)
def test_cast_timestamp_column(t, df, column, to, expected):
    c = t[column].cast(to)
    result = c.execute()
    assert str(result.dtype) == expected


@pytest.mark.parametrize(
    ('to', 'expected'),
    [
        ('string', str),
        ('int64', lambda x: x.value),
        param(
            'double',
            float,
            marks=pytest.mark.xfail(raises=NotImplementedError),
        ),
        (
            dt.Timestamp('America/Los_Angeles'),
            lambda x: x.tz_localize('America/Los_Angeles'),
        ),
    ],
)
def test_cast_timestamp_scalar_naive(to, expected):
    literal_expr = ibis.literal(pd.Timestamp(TIMESTAMP))
    value = literal_expr.cast(to)
    result = execute(value.op())
    raw = execute(literal_expr.op())
    assert result == expected(raw)


@pytest.mark.parametrize(
    ('to', 'expected'),
    [
        ('string', str),
        ('int64', lambda x: x.value),
        param(
            'double',
            float,
            marks=pytest.mark.xfail(raises=NotImplementedError),
        ),
        (
            dt.Timestamp('America/Los_Angeles'),
            lambda x: x.tz_convert('America/Los_Angeles'),
        ),
    ],
)
@pytest.mark.parametrize('tz', ['UTC', 'America/New_York'])
def test_cast_timestamp_scalar(to, expected, tz):
    literal_expr = ibis.literal(pd.Timestamp(TIMESTAMP).tz_localize(tz))
    value = literal_expr.cast(to)
    result = execute(value.op())
    raw = execute(literal_expr.op())
    assert result == expected(raw)


def test_timestamp_with_timezone_is_inferred_correctly(t, df):
    assert t.plain_datetimes_naive.type().equals(dt.timestamp)
    assert t.plain_datetimes_ny.type().equals(dt.Timestamp('America/New_York'))
    assert t.plain_datetimes_utc.type().equals(dt.Timestamp('UTC'))


@pytest.mark.parametrize(
    'column',
    ['plain_datetimes_naive', 'plain_datetimes_ny', 'plain_datetimes_utc'],
)
def test_cast_date(t, df, column):
    expr = t[column].cast('date')
    result = expr.execute()
    expected = df[column].dt.normalize()
    tm.assert_series_equal(result, expected)


@pytest.mark.parametrize('type', [dt.Decimal(9, 0), dt.Decimal(12, 3)])
def test_cast_to_decimal(t, df, type):
    expr = t.float64_as_strings.cast(type)
    result = expr.execute()
    context = decimal.Context(prec=type.precision)
    expected = df.float64_as_strings.apply(
        lambda x: context.create_decimal(x).quantize(
            decimal.Decimal(
                '{}.{}'.format(
                    '0' * (type.precision - type.scale), '0' * type.scale
                )
            )
        )
    )
    tm.assert_series_equal(result, expected)
    assert all(
        abs(element.as_tuple().exponent) == type.scale
        for element in result.values
    )
    assert all(
        1 <= len(element.as_tuple().digits) <= type.precision
        for element in result.values
    )


@pytest.mark.parametrize(
    'column',
    ['plain_int64', 'dup_strings', 'dup_ints', 'strings_with_nulls'],
)
def test_cast_to_category(t, df, column):
    test = t[column].cast('category').execute()
    tm.assert_series_equal(test, df[column].astype('category'))
