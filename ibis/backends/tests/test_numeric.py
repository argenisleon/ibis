import decimal
import math
import operator

import numpy as np
import pandas as pd
import pandas.testing as tm
import pytest
from packaging.version import parse as vparse
from pytest import param

import ibis
from ibis import _
from ibis import literal as L
from ibis.expr import datatypes as dt
from ibis.tests.util import assert_equal

try:
    import duckdb
except ImportError:
    duckdb = None


@pytest.mark.parametrize(
    ('operand_fn', 'expected_operand_fn'),
    [
        param(lambda t: t.float_col, lambda t: t.float_col, id='float-column'),
        param(
            lambda t: t.double_col, lambda t: t.double_col, id='double-column'
        ),
        param(lambda t: ibis.literal(1.3), lambda t: 1.3, id='float-literal'),
        param(
            lambda t: ibis.literal(np.nan),
            lambda t: np.nan,
            id='nan-literal',
        ),
        param(
            lambda t: ibis.literal(np.inf),
            lambda t: np.inf,
            id='inf-literal',
        ),
        param(
            lambda t: ibis.literal(-np.inf),
            lambda t: -np.inf,
            id='-inf-literal',
        ),
    ],
)
@pytest.mark.parametrize(
    ('expr_fn', 'expected_expr_fn'),
    [
        param(operator.methodcaller('isnan'), np.isnan, id='isnan'),
        param(operator.methodcaller('isinf'), np.isinf, id='isinf'),
    ],
)
@pytest.mark.notimpl(["mysql", "sqlite", "datafusion"])
@pytest.mark.xfail(
    duckdb is not None and vparse(duckdb.__version__) < vparse("0.3.3"),
    reason="<0.3.3 does not support isnan/isinf properly",
)
def test_isnan_isinf(
    backend,
    con,
    alltypes,
    df,
    operand_fn,
    expected_operand_fn,
    expr_fn,
    expected_expr_fn,
):
    expr = expr_fn(operand_fn(alltypes)).name('tmp')
    expected = expected_expr_fn(expected_operand_fn(df))

    result = con.execute(expr)

    if isinstance(expected, pd.Series):
        expected = backend.default_series_rename(expected)
        backend.assert_series_equal(result, expected)
    else:
        try:
            assert result == expected
        except ValueError:
            backend.assert_series_equal(result, expected)


@pytest.mark.parametrize(
    ('expr', 'expected'),
    [
        param(L(-5).abs(), 5, id='abs-neg'),
        param(L(5).abs(), 5, id='abs'),
        param(
            ibis.least(L(10), L(1)),
            1,
            id='least',
            marks=pytest.mark.notimpl(["datafusion"]),
        ),
        param(
            ibis.greatest(L(10), L(1)),
            10,
            id='greatest',
            marks=pytest.mark.notimpl(["datafusion"]),
        ),
        param(L(5.5).round(), 6.0, id='round'),
        param(
            L(5.556).round(2),
            5.56,
            id='round-digits',
            marks=pytest.mark.notimpl(["datafusion"]),
        ),
        param(L(5.556).ceil(), 6.0, id='ceil'),
        param(L(5.556).floor(), 5.0, id='floor'),
        param(
            L(5.556).exp(),
            math.exp(5.556),
            id='expr',
            marks=pytest.mark.notimpl(["datafusion"]),
        ),
        param(
            L(5.556).sign(),
            1,
            id='sign-pos',
            marks=pytest.mark.notimpl(["datafusion"]),
        ),
        param(
            L(-5.556).sign(),
            -1,
            id='sign-neg',
            marks=pytest.mark.notimpl(["datafusion"]),
        ),
        param(
            L(0).sign(),
            0,
            id='sign-zero',
            marks=pytest.mark.notimpl(["datafusion"]),
        ),
        param(L(5.556).sqrt(), math.sqrt(5.556), id='sqrt'),
        param(
            L(5.556).log(2),
            math.log(5.556, 2),
            id='log-base',
            marks=pytest.mark.notimpl(["datafusion"]),
        ),
        param(L(5.556).ln(), math.log(5.556), id='ln'),
        param(L(5.556).log2(), math.log(5.556, 2), id='log2'),
        param(L(5.556).log10(), math.log10(5.556), id='log10'),
        param(
            L(5.556).radians(),
            math.radians(5.556),
            id='radians',
            marks=pytest.mark.notimpl(["datafusion", "impala"]),
        ),
        param(
            L(5.556).degrees(),
            math.degrees(5.556),
            id='degrees',
            marks=pytest.mark.notimpl(["datafusion", "impala"]),
        ),
        param(L(11) % 3, 11 % 3, id='mod'),
    ],
)
def test_math_functions_literals(con, expr, expected):
    result = con.execute(expr)
    if isinstance(result, decimal.Decimal):
        # in case of Impala the result is decimal
        # >>> decimal.Decimal('5.56') == 5.56
        # False
        assert result == decimal.Decimal(str(expected))
    else:
        np.testing.assert_allclose(result, expected)


@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        param(L(0.0).acos(), math.acos(0.0), id="acos"),
        param(L(0.0).asin(), math.asin(0.0), id="asin"),
        param(L(0.0).atan(), math.atan(0.0), id="atan"),
        param(L(0.0).atan2(1.0), math.atan2(0.0, 1.0), id="atan2"),
        param(L(0.0).cos(), math.cos(0.0), id="cos"),
        param(L(1.0).cot(), math.cos(1.0) / math.sin(1.0), id="cot"),
        param(L(0.0).sin(), math.sin(0.0), id="sin"),
        param(L(0.0).tan(), math.tan(0.0), id="tan"),
    ],
)
def test_trig_functions_literals(con, expr, expected):
    result = con.execute(expr)
    assert pytest.approx(result) == expected


@pytest.mark.parametrize(
    ("expr", "expected_fn"),
    [
        param(_.dc.acos(), np.arccos, id="acos"),
        param(_.dc.asin(), np.arcsin, id="asin"),
        param(_.dc.atan(), np.arctan, id="atan"),
        param(_.dc.atan2(_.dc), lambda c: np.arctan2(c, c), id="atan2"),
        param(_.dc.cos(), np.cos, id="cos"),
        param(_.dc.cot(), lambda c: 1.0 / np.tan(c), id="cot"),
        param(_.dc.sin(), np.sin, id="sin"),
        param(_.dc.tan(), np.tan, id="tan"),
    ],
)
@pytest.mark.notyet(
    ["datafusion"],
    reason=(
        "datafusion implements trig functions but can't easily test them due"
        " to missing NullIfZero"
    ),
)
def test_trig_functions_columns(backend, expr, alltypes, df, expected_fn):
    dc_max = df.double_col.max()
    expr = alltypes.mutate(dc=(_.double_col / dc_max).nullifzero()).select(
        tmp=expr
    )
    result = expr.tmp.execute()
    expected = expected_fn(
        (df.double_col / dc_max).replace(0.0, np.nan)
    ).rename("tmp")
    backend.assert_series_equal(result, expected)


@pytest.mark.parametrize(
    ('expr_fn', 'expected_fn'),
    [
        param(
            lambda t: (-t.double_col).abs(),
            lambda t: (-t.double_col).abs(),
            id='abs-neg',
        ),
        param(
            lambda t: t.double_col.abs(),
            lambda t: t.double_col.abs(),
            id='abs',
        ),
        param(
            lambda t: t.double_col.ceil(),
            lambda t: np.ceil(t.double_col).astype('int64'),
            id='ceil',
        ),
        param(
            lambda t: t.double_col.floor(),
            lambda t: np.floor(t.double_col).astype('int64'),
            id='floor',
        ),
        param(
            lambda t: t.double_col.sign(),
            lambda t: np.sign(t.double_col),
            id='sign',
            marks=pytest.mark.notimpl(["datafusion"]),
        ),
        param(
            lambda t: (-t.double_col).sign(),
            lambda t: np.sign(-t.double_col),
            id='sign-negative',
            marks=pytest.mark.notimpl(["datafusion"]),
        ),
    ],
)
def test_simple_math_functions_columns(
    backend, con, alltypes, df, expr_fn, expected_fn
):
    expr = expr_fn(alltypes).name('tmp')
    expected = backend.default_series_rename(expected_fn(df))
    result = con.execute(expr)
    backend.assert_series_equal(result, expected)


# we add one to double_col in this test to make sure the common case works (no
# domain errors), and we test the backends' various failure modes in each
# backend's test suite


@pytest.mark.parametrize(
    ('expr_fn', 'expected_fn'),
    [
        param(
            lambda t: t.double_col.add(1).sqrt(),
            lambda t: np.sqrt(t.double_col + 1),
            id='sqrt',
        ),
        param(
            lambda t: t.double_col.add(1).exp(),
            lambda t: np.exp(t.double_col + 1),
            id='exp',
            marks=pytest.mark.notimpl(["datafusion"]),
        ),
        param(
            lambda t: t.double_col.add(1).log(2),
            lambda t: np.log2(t.double_col + 1),
            id='log2',
            marks=pytest.mark.notimpl(["datafusion"]),
        ),
        param(
            lambda t: t.double_col.add(1).ln(),
            lambda t: np.log(t.double_col + 1),
            id='ln',
        ),
        param(
            lambda t: t.double_col.add(1).log10(),
            lambda t: np.log10(t.double_col + 1),
            id='log10',
        ),
        param(
            lambda t: (t.double_col + 1).log(
                ibis.greatest(
                    9_000,
                    t.bigint_col,
                )
            ),
            lambda t: (
                np.log(t.double_col + 1)
                / np.log(np.maximum(9_000, t.bigint_col))
            ),
            id="log_base_bigint",
            marks=pytest.mark.notimpl(["clickhouse", "datafusion"]),
        ),
    ],
)
def test_complex_math_functions_columns(
    backend, con, alltypes, df, expr_fn, expected_fn
):
    expr = expr_fn(alltypes).name('tmp')
    expected = backend.default_series_rename(expected_fn(df))
    result = con.execute(expr)
    backend.assert_series_equal(result, expected)


@pytest.mark.parametrize(
    ('expr_fn', 'expected_fn'),
    [
        param(
            lambda be, t: t.double_col.round(),
            lambda be, t: be.round(t.double_col),
            id='round',
        ),
        param(
            lambda be, t: t.double_col.add(0.05).round(3),
            lambda be, t: be.round(t.double_col + 0.05, 3),
            id='round-with-param',
            marks=pytest.mark.notimpl(["datafusion"]),
        ),
        param(
            lambda be, t: be.least(ibis.least, t.bigint_col, t.int_col),
            lambda be, t: pd.Series(list(map(min, t.bigint_col, t.int_col))),
            id='least-all-columns',
            marks=pytest.mark.notimpl(["datafusion"]),
        ),
        param(
            lambda be, t: be.least(ibis.least, t.bigint_col, t.int_col, -2),
            lambda be, t: pd.Series(
                list(map(min, t.bigint_col, t.int_col, [-2] * len(t)))
            ),
            id='least-scalar',
            marks=pytest.mark.notimpl(["datafusion", "clickhouse"]),
        ),
        param(
            lambda be, t: be.greatest(ibis.greatest, t.bigint_col, t.int_col),
            lambda be, t: pd.Series(list(map(max, t.bigint_col, t.int_col))),
            id='greatest-all-columns',
            marks=pytest.mark.notimpl(["datafusion"]),
        ),
        param(
            lambda be, t: be.greatest(
                ibis.greatest, t.bigint_col, t.int_col, -2
            ),
            lambda be, t: pd.Series(
                list(map(max, t.bigint_col, t.int_col, [-2] * len(t)))
            ),
            id='greatest-scalar',
            marks=pytest.mark.notimpl(["datafusion", "clickhouse"]),
        ),
    ],
)
def test_backend_specific_numerics(
    backend, con, df, alltypes, expr_fn, expected_fn
):
    expr = expr_fn(backend, alltypes)
    result = backend.default_series_rename(con.execute(expr))
    expected = backend.default_series_rename(expected_fn(backend, df))
    backend.assert_series_equal(result, expected)


# marks=pytest.mark.notimpl(["datafusion"]),
@pytest.mark.parametrize(
    'op',
    [
        operator.add,
        operator.sub,
        operator.mul,
        operator.truediv,
        operator.floordiv,
        param(
            operator.pow,
            marks=pytest.mark.notimpl(["datafusion"]),
        ),
    ],
    ids=lambda op: op.__name__,
)
def test_binary_arithmetic_operations(backend, alltypes, df, op):
    smallint_col = alltypes.smallint_col + 1  # make it nonzero
    smallint_series = df.smallint_col + 1

    expr = op(alltypes.double_col, smallint_col).name('tmp')

    result = expr.execute()
    expected = op(df.double_col, smallint_series)
    if op is operator.floordiv:
        # defined in ops.FloorDivide.output_type
        # -> returns int64 whereas pandas float64
        result = result.astype('float64')

    expected = backend.default_series_rename(expected)
    backend.assert_series_equal(result, expected, check_exact=False)


def test_mod(backend, alltypes, df):
    expr = operator.mod(alltypes.smallint_col, alltypes.smallint_col + 1).name(
        'tmp'
    )

    result = expr.execute()
    expected = operator.mod(df.smallint_col, df.smallint_col + 1)

    expected = backend.default_series_rename(expected)
    backend.assert_series_equal(result, expected, check_dtype=False)


def test_floating_mod(backend, alltypes, df):
    expr = operator.mod(alltypes.double_col, alltypes.smallint_col + 1).name(
        'tmp'
    )

    result = expr.execute()
    expected = operator.mod(df.double_col, df.smallint_col + 1)

    expected = backend.default_series_rename(expected)
    backend.assert_series_equal(result, expected, check_exact=False)


@pytest.mark.parametrize(
    'column',
    [
        'tinyint_col',
        'smallint_col',
        'int_col',
        'bigint_col',
        'float_col',
        'double_col',
    ],
)
@pytest.mark.notyet(
    [
        "datafusion",
        "duckdb",
        "mysql",
        "postgres",
        "pyspark",
        "sqlite",
        "snowflake",
    ]
)
@pytest.mark.parametrize('denominator', [0, 0.0])
def test_divide_by_zero(backend, alltypes, df, column, denominator):
    expr = alltypes[column] / denominator
    result = expr.name('tmp').execute()

    expected = df[column].div(denominator)
    expected = backend.default_series_rename(expected)

    backend.assert_series_equal(result, expected)


@pytest.mark.parametrize(
    ("default_precisions", "default_scales"),
    [
        (
            {'postgres': None, 'mysql': 10, 'snowflake': 38},
            {'postgres': None, 'mysql': 0, 'snowflake': 0},
        )
    ],
)
@pytest.mark.notimpl(["sqlite", "duckdb"])
@pytest.mark.never(
    ["clickhouse", "dask", "datafusion", "impala", "pandas", "pyspark"],
    reason="Not SQLAlchemy backends",
)
def test_sa_default_numeric_precision_and_scale(
    con, backend, default_precisions, default_scales
):
    sa = pytest.importorskip("sqlalchemy")
    # TODO: find a better way to access ibis.sql.alchemy
    from ibis.backends.base.sql.alchemy import schema_from_table

    default_precision = default_precisions[backend.name()]
    default_scale = default_scales[backend.name()]

    typespec = [
        # name, sqlalchemy type, ibis type
        ('n1', sa.NUMERIC, dt.Decimal(default_precision, default_scale)),
        ('n2', sa.NUMERIC(5), dt.Decimal(5, default_scale)),
        ('n3', sa.NUMERIC(None, 4), dt.Decimal(default_precision, 4)),
        ('n4', sa.NUMERIC(10, 2), dt.Decimal(10, 2)),
    ]

    sqla_types = []
    ibis_types = []
    for name, t, ibis_type in typespec:
        sqla_type = sa.Column(name, t, nullable=True)
        sqla_types.append(sqla_type)
        ibis_types.append((name, ibis_type(nullable=True)))

    # Create a table with the numeric types.
    table_name = 'test_sa_default_param_decimal'
    engine = con.con
    table = sa.Table(table_name, sa.MetaData(bind=engine), *sqla_types)
    table.create(bind=engine)

    try:
        # Check that we can correctly recover the default precision and scale.
        schema = schema_from_table(table)
        expected = ibis.schema(ibis_types)

        assert_equal(schema, expected)
    finally:
        con.drop_table(table_name, force=True)


@pytest.mark.notimpl(["dask", "datafusion", "impala", "pandas", "sqlite"])
@pytest.mark.notyet(
    ["clickhouse", "snowflake"],
    reason="backend doesn't implement a [0.0, 1.0) random function",
)
def test_random(con):
    expr = ibis.random()
    result = con.execute(expr)
    assert isinstance(result, float)
    assert 0 <= result < 1

    # Ensure a different random number of produced on subsequent calls
    result2 = con.execute(expr)
    assert isinstance(result2, float)
    assert 0 <= result2 < 1
    assert result != result2


@pytest.mark.parametrize(
    ('ibis_func', 'pandas_func'),
    [
        (lambda x: x.clip(lower=0), lambda x: x.clip(lower=0)),
        (lambda x: x.clip(lower=0.0), lambda x: x.clip(lower=0.0)),
        (lambda x: x.clip(upper=0), lambda x: x.clip(upper=0)),
        (
            lambda x: x.clip(lower=x - 1, upper=x + 1),
            lambda x: x.clip(lower=x - 1, upper=x + 1),
        ),
        (
            lambda x: x.clip(lower=0, upper=1),
            lambda x: x.clip(lower=0, upper=1),
        ),
        (
            lambda x: x.clip(lower=0, upper=1.0),
            lambda x: x.clip(lower=0, upper=1.0),
        ),
    ],
)
@pytest.mark.notimpl(["datafusion", "impala"])
def test_clip(alltypes, df, ibis_func, pandas_func):
    result = ibis_func(alltypes.int_col).execute()
    expected = pandas_func(df.int_col).astype(result.dtype)
    # Names won't match in the Pyspark backend since Pyspark
    # gives 'tmp' name when executing a Column
    tm.assert_series_equal(result, expected, check_names=False)


@pytest.mark.notimpl(
    [
        "dask",
        "datafusion",
        "pandas",
        "pyspark",
    ]
)
def test_histogram(con, alltypes):
    n = 10
    results = con.execute(alltypes.int_col.histogram(n))
    assert len(results.value_counts()) == n
