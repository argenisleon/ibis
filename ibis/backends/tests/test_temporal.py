import datetime
import operator
import warnings
from operator import methodcaller

import numpy as np
import pandas as pd
import pandas.testing as tm
import pytest
from pytest import param

import ibis
import ibis.common.exceptions as com
import ibis.expr.datatypes as dt
from ibis.backends.pandas.execution.temporal import day_name


@pytest.mark.parametrize('attr', ['year', 'month', 'day'])
@pytest.mark.parametrize(
    "expr_fn",
    [
        param(lambda c: c.date(), id="date"),
        param(
            lambda c: c.cast("date"),
            id="cast",
            marks=pytest.mark.notimpl(["impala"]),
        ),
    ],
)
@pytest.mark.notimpl(["datafusion"])
def test_date_extract(backend, alltypes, df, attr, expr_fn):
    expr = getattr(expr_fn(alltypes.timestamp_col), attr)()
    expected = getattr(df.timestamp_col.dt, attr).astype('int32')

    result = expr.name(attr).execute()

    backend.assert_series_equal(result, expected.rename(attr))


@pytest.mark.parametrize(
    'attr',
    [
        'year',
        'month',
        'day',
        param('day_of_year', marks=pytest.mark.notimpl(["impala"])),
        'quarter',
        'hour',
        'minute',
        'second',
    ],
)
@pytest.mark.notimpl(["datafusion"])
def test_timestamp_extract(backend, alltypes, df, attr):
    method = getattr(alltypes.timestamp_col, attr)
    expr = method().name(attr)
    result = expr.execute()
    expected = backend.default_series_rename(
        getattr(df.timestamp_col.dt, attr.replace('_', '')).astype('int32')
    ).rename(attr)
    backend.assert_series_equal(result, expected)


@pytest.mark.parametrize(
    ('func', 'expected'),
    [
        param(methodcaller('year'), 2015, id='year'),
        param(methodcaller('month'), 9, id='month'),
        param(methodcaller('day'), 1, id='day'),
        param(methodcaller('hour'), 14, id='hour'),
        param(methodcaller('minute'), 48, id='minute'),
        param(methodcaller('second'), 5, id='second'),
        param(
            methodcaller('millisecond'),
            359,
            id='millisecond',
            marks=[
                pytest.mark.notimpl(["clickhouse", "pyspark"]),
                pytest.mark.broken(
                    ["mysql"],
                    reason="MySQL implementation of milliseconds is broken",
                ),
            ],
        ),
        param(lambda x: x.day_of_week.index(), 1, id='day_of_week_index'),
        param(
            lambda x: x.day_of_week.full_name(),
            'Tuesday',
            id='day_of_week_full_name',
        ),
    ],
)
@pytest.mark.notimpl(["datafusion", "snowflake"])
def test_timestamp_extract_literal(con, func, expected):
    value = ibis.timestamp('2015-09-01 14:48:05.359')
    assert con.execute(func(value)) == expected


@pytest.mark.notimpl(["datafusion", "clickhouse", "snowflake"])
@pytest.mark.notyet(["sqlite", "pyspark"])
def test_timestamp_extract_milliseconds(backend, alltypes, df):
    expr = alltypes.timestamp_col.millisecond()
    result = expr.execute()
    expected = backend.default_series_rename(
        (df.timestamp_col.dt.microsecond // 1_000).astype('int32')
    ).rename("millisecond")
    backend.assert_series_equal(result, expected)


@pytest.mark.notimpl(["datafusion"])
def test_timestamp_extract_epoch_seconds(backend, alltypes, df):
    expr = alltypes.timestamp_col.epoch_seconds().name('tmp')
    result = expr.execute()

    expected = backend.default_series_rename(
        (df.timestamp_col.view("int64") // 1_000_000_000).astype("int32")
    )
    backend.assert_series_equal(result, expected)


@pytest.mark.notimpl(["datafusion"])
def test_timestamp_extract_week_of_year(backend, alltypes, df):
    expr = alltypes.timestamp_col.week_of_year().name('tmp')
    result = expr.execute()
    expected = backend.default_series_rename(
        df.timestamp_col.dt.isocalendar().week.astype("int32")
    )
    backend.assert_series_equal(result, expected)


@pytest.mark.parametrize(
    'unit',
    [
        'Y',
        'M',
        'D',
        param(
            'W',
            marks=pytest.mark.notimpl(
                [
                    "clickhouse",
                    "duckdb",
                    "impala",
                    "mysql",
                    "postgres",
                    "pyspark",
                    "sqlite",
                    "snowflake",
                ]
            ),
        ),
        param('h', marks=pytest.mark.notimpl(["sqlite"])),
        param('m', marks=pytest.mark.notimpl(["sqlite"])),
        param('s', marks=pytest.mark.notimpl(["impala", "sqlite"])),
        param(
            'ms',
            marks=pytest.mark.notimpl(
                [
                    "clickhouse",
                    "impala",
                    "mysql",
                    "pyspark",
                    "sqlite",
                ]
            ),
        ),
        param(
            'us',
            marks=pytest.mark.notimpl(
                [
                    "clickhouse",
                    "impala",
                    "mysql",
                    "pyspark",
                    "sqlite",
                ]
            ),
        ),
        param(
            'ns',
            marks=pytest.mark.notimpl(
                [
                    "clickhouse",
                    "duckdb",
                    "impala",
                    "mysql",
                    "postgres",
                    "pyspark",
                    "sqlite",
                    "snowflake",
                ]
            ),
        ),
    ],
)
@pytest.mark.notimpl(["datafusion"])
def test_timestamp_truncate(backend, alltypes, df, unit):
    expr = alltypes.timestamp_col.truncate(unit).name('tmp')

    dtype = f'datetime64[{unit}]'
    expected = pd.Series(df.timestamp_col.values.astype(dtype))

    result = expr.execute()
    expected = backend.default_series_rename(expected)

    backend.assert_series_equal(result, expected)


@pytest.mark.parametrize(
    'unit',
    [
        'Y',
        'M',
        'D',
        param(
            'W',
            marks=pytest.mark.notimpl(
                [
                    "clickhouse",
                    "duckdb",
                    "impala",
                    "mysql",
                    "postgres",
                    "pyspark",
                    "sqlite",
                    "snowflake",
                ]
            ),
        ),
    ],
)
@pytest.mark.notimpl(["datafusion"])
def test_date_truncate(backend, alltypes, df, unit):
    expr = alltypes.timestamp_col.date().truncate(unit).name('tmp')

    dtype = f"datetime64[{unit}]"
    expected = pd.Series(df.timestamp_col.values.astype(dtype))

    result = expr.execute()
    expected = backend.default_series_rename(expected)

    backend.assert_series_equal(result, expected)


@pytest.mark.parametrize(
    ('unit', 'displacement_type'),
    [
        param(
            'Y',
            pd.offsets.DateOffset,
            # TODO - DateOffset - #2553
            marks=pytest.mark.notimpl(['dask']),
        ),
        param('Q', pd.offsets.DateOffset, marks=pytest.mark.xfail),
        param(
            'M',
            pd.offsets.DateOffset,
            # TODO - DateOffset - #2553
            marks=pytest.mark.notimpl(['dask']),
        ),
        param(
            'W',
            pd.offsets.DateOffset,
            # TODO - DateOffset - #2553
            marks=pytest.mark.notimpl(['dask']),
        ),
        param('D', pd.offsets.DateOffset),
        param('h', pd.Timedelta),
        param('m', pd.Timedelta),
        param('s', pd.Timedelta),
        param(
            'ms',
            pd.Timedelta,
            marks=pytest.mark.notimpl(["clickhouse", "mysql"]),
        ),
        param(
            'us',
            pd.Timedelta,
            marks=pytest.mark.notimpl(["clickhouse"]),
        ),
    ],
)
@pytest.mark.notimpl(["datafusion", "pyspark", "sqlite", "snowflake"])
def test_integer_to_interval_timestamp(
    backend, con, alltypes, df, unit, displacement_type
):
    interval = alltypes.int_col.to_interval(unit=unit)
    expr = (alltypes.timestamp_col + interval).name('tmp')

    def convert_to_offset(offset, displacement_type=displacement_type):
        resolution = f'{interval.type().resolution}s'
        return displacement_type(**{resolution: offset})

    with warnings.catch_warnings():
        # both the implementation and test code raises pandas
        # PerformanceWarning, because We use DateOffset addition
        warnings.simplefilter("ignore", category=pd.errors.PerformanceWarning)
        result = con.execute(expr)
        offset = df.int_col.apply(convert_to_offset)
        expected = df.timestamp_col + offset

    expected = backend.default_series_rename(expected)
    backend.assert_series_equal(result, expected)


@pytest.mark.parametrize(
    'unit', ['Y', param('Q', marks=pytest.mark.xfail), 'M', 'W', 'D']
)
# TODO - DateOffset - #2553
@pytest.mark.notimpl(
    [
        "dask",
        "datafusion",
        "impala",
        "mysql",
        "pyspark",
        "sqlite",
        "snowflake",
    ]
)
def test_integer_to_interval_date(backend, con, alltypes, df, unit):
    interval = alltypes.int_col.to_interval(unit=unit)
    array = alltypes.date_string_col.split('/')
    month, day, year = array[0], array[1], array[2]
    date_col = expr = (
        ibis.literal('-').join(['20' + year, month, day]).cast('date')
    )
    expr = (date_col + interval).name('tmp')
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=pd.errors.PerformanceWarning)
        result = con.execute(expr)

    def convert_to_offset(x):
        resolution = f'{interval.type().resolution}s'
        return pd.offsets.DateOffset(**{resolution: x})

    offset = df.int_col.apply(convert_to_offset)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=pd.errors.PerformanceWarning)
        expected = pd.to_datetime(df.date_string_col) + offset
    expected = backend.default_series_rename(expected)

    backend.assert_series_equal(result, expected)


date_value = pd.Timestamp('2017-12-31')
timestamp_value = pd.Timestamp('2018-01-01 18:18:18')


@pytest.mark.parametrize(
    ('expr_fn', 'expected_fn'),
    [
        param(
            lambda t, _: t.timestamp_col + ibis.interval(days=4),
            lambda t, _: t.timestamp_col + pd.Timedelta(days=4),
            id='timestamp-add-interval',
        ),
        param(
            lambda t, _: t.timestamp_col
            + (ibis.interval(days=4) - ibis.interval(days=2)),
            lambda t, _: t.timestamp_col
            + (pd.Timedelta(days=4) - pd.Timedelta(days=2)),
            id='timestamp-add-interval-binop',
            marks=pytest.mark.notimpl(
                [
                    "clickhouse",
                    "dask",
                    "duckdb",
                    "impala",
                    "mysql",
                    "pandas",
                    "postgres",
                    "snowflake",
                ]
            ),
        ),
        param(
            lambda t, _: t.timestamp_col - ibis.interval(days=17),
            lambda t, _: t.timestamp_col - pd.Timedelta(days=17),
            id='timestamp-subtract-interval',
        ),
        param(
            lambda t, _: t.timestamp_col.date() + ibis.interval(days=4),
            lambda t, _: t.timestamp_col.dt.floor('d') + pd.Timedelta(days=4),
            id='date-add-interval',
        ),
        param(
            lambda t, _: t.timestamp_col.date() - ibis.interval(days=14),
            lambda t, _: t.timestamp_col.dt.floor('d') - pd.Timedelta(days=14),
            id='date-subtract-interval',
        ),
        param(
            lambda t, _: t.timestamp_col - ibis.timestamp(timestamp_value),
            lambda t, be: pd.Series(
                t.timestamp_col.sub(timestamp_value).values.astype(
                    f'timedelta64[{be.returned_timestamp_unit}]'
                )
            ),
            id='timestamp-subtract-timestamp',
            marks=pytest.mark.notimpl(["duckdb", "pyspark", "snowflake"]),
        ),
        param(
            lambda t, _: t.timestamp_col.date() - ibis.date(date_value),
            lambda t, _: t.timestamp_col.dt.floor('d') - date_value,
            id='date-subtract-date',
            marks=pytest.mark.notimpl(["pyspark", "snowflake"]),
        ),
    ],
)
@pytest.mark.notimpl(["datafusion", "sqlite"])
def test_temporal_binop(backend, con, alltypes, df, expr_fn, expected_fn):
    expr = expr_fn(alltypes, backend).name('tmp')
    expected = expected_fn(df, backend)

    result = con.execute(expr)
    expected = backend.default_series_rename(expected)

    backend.assert_series_equal(result, expected)


plus = lambda t, td: t.timestamp_col + pd.Timedelta(td)  # noqa: E731
minus = lambda t, td: t.timestamp_col - pd.Timedelta(td)  # noqa: E731


@pytest.mark.parametrize(
    ('timedelta', 'temporal_fn'),
    [
        ('36500d', plus),
        ('5W', plus),
        ('3d', plus),
        param('1.5d', plus, marks=pytest.mark.notimpl(["mysql"])),
        param('2h', plus, marks=pytest.mark.notimpl(["mysql"])),
        param('3m', plus, marks=pytest.mark.notimpl(["mysql"])),
        param('10s', plus, marks=pytest.mark.notimpl(["mysql"])),
        ('36500d', minus),
        ('5W', minus),
        ('3d', minus),
        param('1.5d', minus, marks=pytest.mark.notimpl(["mysql"])),
        param('2h', minus, marks=pytest.mark.notimpl(["mysql"])),
        param('3m', minus, marks=pytest.mark.notimpl(["mysql"])),
        param('10s', minus, marks=pytest.mark.notimpl(["mysql"])),
    ],
)
@pytest.mark.notimpl(
    ["clickhouse", "datafusion", "impala", "sqlite", "snowflake"]
)
def test_temporal_binop_pandas_timedelta(
    backend, con, alltypes, df, timedelta, temporal_fn
):
    expr = temporal_fn(alltypes, timedelta).name('tmp')
    expected = temporal_fn(df, timedelta)

    result = con.execute(expr)
    expected = backend.default_series_rename(expected)

    backend.assert_series_equal(result, expected)


@pytest.mark.parametrize(
    'comparison_fn',
    [
        operator.gt,
        operator.ge,
        operator.lt,
        operator.le,
        operator.eq,
        operator.ne,
    ],
)
def test_timestamp_comparison_filter(
    backend, con, alltypes, df, comparison_fn
):
    ts = pd.Timestamp('20100302', tz="UTC").to_pydatetime()
    expr = alltypes.filter(
        comparison_fn(alltypes.timestamp_col.cast("timestamp('UTC')"), ts)
    )

    col = df.timestamp_col.dt.tz_localize("UTC")
    expected = df[comparison_fn(col, ts)]
    result = con.execute(expr)

    backend.assert_frame_equal(result, expected)


@pytest.mark.notimpl(["datafusion", "sqlite", "snowflake"])
def test_interval_add_cast_scalar(backend, alltypes):
    timestamp_date = alltypes.timestamp_col.date()
    delta = ibis.literal(10).cast("interval('D')")
    expr = (timestamp_date + delta).name('result')
    result = expr.execute()
    expected = timestamp_date.name('result').execute() + pd.Timedelta(
        10, unit='D'
    )
    backend.assert_series_equal(result, expected)


@pytest.mark.never(
    ['pyspark'], reason="PySpark does not support casting columns to intervals"
)
@pytest.mark.notimpl(["datafusion", "sqlite", "snowflake"])
def test_interval_add_cast_column(backend, alltypes, df):
    timestamp_date = alltypes.timestamp_col.date()
    delta = alltypes.bigint_col.cast("interval('D')")
    expr = alltypes['id', (timestamp_date + delta).name('tmp')]
    result = expr.execute().sort_values('id').reset_index().tmp
    df = df.sort_values('id').reset_index(drop=True)
    expected = (
        df['timestamp_col']
        .dt.normalize()
        .add(df.bigint_col.astype("timedelta64[D]"))
        .rename("tmp")
    )
    backend.assert_series_equal(result, expected)


@pytest.mark.parametrize(
    ('expr_fn', 'pandas_pattern'),
    [
        param(
            lambda t: t.timestamp_col.strftime('%Y%m%d').name("formatted"),
            '%Y%m%d',
            id="literal_format_str",
        ),
        param(
            lambda t: (
                t.mutate(suffix="%d")
                .select(
                    [
                        lambda t: t.timestamp_col.strftime(
                            "%Y%m" + t.suffix
                        ).name("formatted")
                    ]
                )
                .formatted
            ),
            '%Y%m%d',
            marks=[
                pytest.mark.notimpl(
                    ["dask", "pandas", "postgres", "pyspark", "snowflake"]
                ),
                pytest.mark.notyet(["duckdb", "impala"]),
            ],
            id="column_format_str",
        ),
    ],
)
@pytest.mark.notimpl(["datafusion"])
def test_strftime(backend, alltypes, df, expr_fn, pandas_pattern):
    expr = expr_fn(alltypes)
    expected = df.timestamp_col.dt.strftime(pandas_pattern).rename("formatted")

    result = expr.execute()
    backend.assert_series_equal(result, expected)


unit_factors = {'s': int(1e9), 'ms': int(1e6), 'us': int(1e3), 'ns': 1}


@pytest.mark.parametrize(
    'unit',
    [
        's',
        param(
            'ms',
            marks=pytest.mark.notimpl(["clickhouse", "pyspark"]),
        ),
        param(
            'us',
            marks=pytest.mark.notimpl(["clickhouse", "duckdb", "pyspark"]),
        ),
        param(
            'ns',
            marks=pytest.mark.notimpl(["clickhouse", "duckdb", "pyspark"]),
        ),
    ],
)
@pytest.mark.notimpl(
    ["datafusion", "mysql", "postgres", "sqlite", "snowflake"]
)
def test_integer_to_timestamp(backend, con, unit):
    backend_unit = backend.returned_timestamp_unit
    factor = unit_factors[unit]

    ts = ibis.timestamp('2018-04-13 09:54:11.872832')
    pandas_ts = ibis.pandas.execute(ts).floor(unit).value

    # convert the now timestamp to the input unit being tested
    int_expr = ibis.literal(pandas_ts // factor)
    expr = int_expr.to_timestamp(unit)
    result = con.execute(expr)
    expected = pd.Timestamp(pandas_ts, unit='ns').floor(backend_unit)

    assert result == expected


@pytest.mark.parametrize(
    'fmt, timezone',
    [
        # "11/01/10" - "month/day/year"
        param(
            '%m/%d/%y',
            "UTC",
            id="mysql_format",
            marks=pytest.mark.never(
                ["pyspark"], reason="datetime formatting style not supported"
            ),
        ),
        param(
            'MM/dd/yy',
            "UTC",
            id="pyspark_format",
            marks=pytest.mark.never(
                ["mysql"], reason="datetime formatting style not supported"
            ),
        ),
    ],
)
@pytest.mark.notimpl(
    [
        'dask',
        'pandas',
        'postgres',
        'duckdb',
        'clickhouse',
        'sqlite',
        'impala',
        'datafusion',
        'snowflake',
    ]
)
def test_string_to_timestamp(alltypes, fmt, timezone):
    table = alltypes
    result = table.mutate(
        date=table.date_string_col.to_timestamp(fmt, timezone)
    ).execute()

    # TEST: do we get the same date out, that we put in?
    # format string assumes that we are using pandas' strftime
    for i, val in enumerate(result["date"]):
        assert val.strftime("%m/%d/%y") == result["date_string_col"][i]


@pytest.mark.notimpl(
    [
        'dask',
        'pandas',
        'postgres',
        'duckdb',
        'clickhouse',
        'sqlite',
        'impala',
        'datafusion',
        'snowflake',
    ]
)
def test_string_to_timestamp_tz_error(alltypes):
    table = alltypes

    with pytest.raises(com.UnsupportedArgumentError):
        table.mutate(
            date=table.date_string_col.to_timestamp(
                "%m/%d/%y", 'non-utc-timezone'
            )
        ).compile()


@pytest.mark.parametrize(
    ('date', 'expected_index', 'expected_day'),
    [
        param('2017-01-01', 6, 'Sunday', id="sunday"),
        param('2017-01-02', 0, 'Monday', id="monday"),
        param('2017-01-03', 1, 'Tuesday', id="tuesday"),
        param('2017-01-04', 2, 'Wednesday', id="wednesday"),
        param('2017-01-05', 3, 'Thursday', id="thursday"),
        param('2017-01-06', 4, 'Friday', id="friday"),
        param('2017-01-07', 5, 'Saturday', id="saturday"),
    ],
)
@pytest.mark.notimpl(["datafusion", "impala", "snowflake"])
def test_day_of_week_scalar(con, date, expected_index, expected_day):
    expr = ibis.literal(date).cast(dt.date)
    result_index = con.execute(expr.day_of_week.index())
    assert result_index == expected_index

    result_day = con.execute(expr.day_of_week.full_name())
    assert result_day.lower() == expected_day.lower()


@pytest.mark.notimpl(["datafusion", "snowflake"])
def test_day_of_week_column(backend, alltypes, df):
    expr = alltypes.timestamp_col.day_of_week

    result_index = expr.index().execute()
    expected_index = df.timestamp_col.dt.dayofweek.astype('int16')

    backend.assert_series_equal(
        result_index, expected_index, check_names=False
    )

    result_day = expr.full_name().execute()
    expected_day = day_name(df.timestamp_col.dt)

    backend.assert_series_equal(result_day, expected_day, check_names=False)


@pytest.mark.parametrize(
    ('day_of_week_expr', 'day_of_week_pandas'),
    [
        param(
            lambda t: t.timestamp_col.day_of_week.index().count(),
            lambda s: s.dt.dayofweek.count(),
            id="day_of_week_index",
        ),
        param(
            lambda t: t.timestamp_col.day_of_week.full_name().length().sum(),
            lambda s: day_name(s.dt).str.len().sum(),
            id="day_of_week_full_name",
            marks=[pytest.mark.notimpl(["snowflake"])],
        ),
    ],
)
@pytest.mark.notimpl(["datafusion"])
def test_day_of_week_column_group_by(
    backend, alltypes, df, day_of_week_expr, day_of_week_pandas
):
    expr = alltypes.groupby('string_col').aggregate(
        day_of_week_result=day_of_week_expr
    )
    schema = expr.schema()
    assert schema['day_of_week_result'] == dt.int64

    result = expr.execute().sort_values('string_col')
    expected = (
        df.groupby('string_col')
        .timestamp_col.apply(day_of_week_pandas)
        .reset_index()
        .rename(columns={'timestamp_col': 'day_of_week_result'})
    )

    # FIXME(#1536): Pandas backend should use query.schema().apply_to
    backend.assert_frame_equal(result, expected, check_dtype=False)


@pytest.mark.notimpl(["datafusion", "snowflake"])
def test_now(con):
    expr = ibis.now()
    result = con.execute(expr)
    assert isinstance(result, pd.Timestamp)

    pattern = "%Y%m%d %H"
    result_strftime = con.execute(expr.strftime(pattern))
    expected_strftime = datetime.datetime.utcnow().strftime(pattern)
    assert result_strftime == expected_strftime


@pytest.mark.notimpl(["dask"], reason="Limit #2553")
@pytest.mark.notimpl(["datafusion", "snowflake"])
def test_now_from_projection(alltypes):
    n = 5
    expr = alltypes[[ibis.now().name('ts')]].limit(n)
    result = expr.execute()
    ts = result.ts
    assert isinstance(result, pd.DataFrame)
    assert isinstance(ts, pd.Series)
    assert issubclass(ts.dtype.type, np.datetime64)
    assert len(result) == n
    assert ts.nunique() == 1

    now = pd.Timestamp('now')
    year_expected = pd.Series([now.year] * n, name='ts')
    tm.assert_series_equal(ts.dt.year, year_expected)


@pytest.mark.notimpl(
    ["pandas", "datafusion", "mysql", "dask", "pyspark", "snowflake"]
)
@pytest.mark.notyet(["clickhouse", "impala"])
def test_date_literal(con):
    expr = ibis.date(2022, 2, 4)
    result = con.execute(expr)
    assert result.strftime('%Y-%m-%d') == '2022-02-04'


@pytest.mark.notimpl(
    ["pandas", "datafusion", "mysql", "dask", "pyspark", "snowflake"]
)
@pytest.mark.notyet(["clickhouse", "impala"])
def test_timestamp_literal(con):
    expr = ibis.timestamp(2022, 2, 4, 16, 20, 0)
    result = con.execute(expr)
    if not isinstance(result, str):
        result = result.strftime('%Y-%m-%d %H:%M:%S%Z')
    assert result == '2022-02-04 16:20:00'


@pytest.mark.notimpl(
    ["pandas", "datafusion", "mysql", "dask", "pyspark", "snowflake"]
)
@pytest.mark.notyet(["clickhouse", "impala"])
def test_time_literal(con):
    expr = ibis.time(16, 20, 0)
    result = con.execute(expr)
    if not isinstance(result, str):
        result = result.strftime('%H:%M:%S')
    assert result == '16:20:00'


@pytest.mark.notimpl(
    ["pandas", "datafusion", "mysql", "dask", "pyspark", "snowflake"]
)
@pytest.mark.notyet(["clickhouse", "impala"])
def test_date_column_from_ymd(con, alltypes, df):
    c = alltypes.timestamp_col
    expr = ibis.date(c.year(), c.month(), c.day())
    tbl = alltypes[
        expr.name('timestamp_col'),
    ]
    result = con.execute(tbl)

    golden = df.timestamp_col.dt.date.astype('datetime64[ns]')
    tm.assert_series_equal(golden, result.timestamp_col)


@pytest.mark.notimpl(["datafusion", "impala"])
def test_date_scalar_from_iso(con):
    expr = ibis.literal('2022-02-24')
    expr2 = ibis.date(expr)

    result = con.execute(expr2)
    assert result.strftime('%Y-%m-%d') == '2022-02-24'


@pytest.mark.notimpl(["datafusion", "impala", "pyspark"])
def test_date_column_from_iso(con, alltypes, df):
    expr = (
        alltypes.year.cast('string')
        + '-'
        + alltypes.month.cast('string').lpad(2, '0')
        + '-13'
    )
    expr = ibis.date(expr)

    result = con.execute(expr)
    golden = (
        df.year.astype(str)
        + '-'
        + df.month.astype(str).str.rjust(2, '0')
        + '-13'
    )
    actual = result.dt.strftime('%Y-%m-%d')
    tm.assert_series_equal(golden.rename('tmp'), actual.rename('tmp'))


@pytest.mark.notimpl(["datafusion", "snowflake"])
@pytest.mark.notyet(["clickhouse", "pyspark"])
@pytest.mark.broken(["mysql"])
def test_timestamp_extract_milliseconds_with_big_value(con):
    timestamp = ibis.timestamp("2021-01-01 01:30:59.333")
    millis = timestamp.millisecond()
    result = con.execute(millis)
    assert result == 333


@pytest.mark.notimpl(["datafusion"])
@pytest.mark.broken(
    ["dask", "pandas"],
    reason="Pandas and Dask interpret integers as nanoseconds since epoch",
)
def test_integer_cast_to_timestamp(backend, alltypes, df):
    expr = alltypes.int_col.cast("timestamp")
    expected = pd.to_datetime(df.int_col, unit="s").rename(expr.get_name())
    result = expr.execute()
    backend.assert_series_equal(result, expected)


@pytest.mark.broken(
    ["clickhouse", "impala"],
    reason=(
        "Impala returns a string; "
        "the clickhouse driver returns invalid results for big timestamps"
    ),
)
@pytest.mark.notimpl(
    ["datafusion"],
    reason="DataFusion backend assumes ns resolution timestamps",
)
@pytest.mark.notyet(
    ["pyspark"],
    reason="PySpark doesn't handle big timestamps",
)
@pytest.mark.notimpl(["snowflake"])
def test_big_timestamp(con):
    # TODO: test with a timezone
    value = ibis.timestamp("2419-10-11 10:10:25")
    result = con.execute(value)
    expected = datetime.datetime(2419, 10, 11, 10, 10, 25)
    assert result == expected


DATE = datetime.date(2010, 11, 1)


def build_date_col(t):
    return (
        t.year.cast("string")
        + "-"
        + t.month.cast("string").lpad(2, "0")
        + "-"
        + (t.int_col + 1).cast("string").lpad(2, "0")
    ).cast("date")


@pytest.mark.notimpl(["datafusion"])
@pytest.mark.notyet(["impala"], reason="impala doesn't support dates")
@pytest.mark.parametrize(
    ("left_fn", "right_fn"),
    [
        param(build_date_col, lambda _: DATE, id="column_date"),
        param(lambda _: DATE, build_date_col, id="date_column"),
    ],
)
def test_timestamp_date_comparison(backend, alltypes, df, left_fn, right_fn):
    left = left_fn(alltypes)
    right = right_fn(alltypes)
    expr = left == right
    result = expr.execute().rename("result")
    expected = (
        pd.to_datetime(
            (
                df.year.astype(str)
                .add("-")
                .add(df.month.astype(str).str.rjust(2, "0"))
                .add("-")
                .add(df.int_col.add(1).astype(str).str.rjust(2, "0"))
            ),
            format="%Y-%m-%d",
            exact=True,
        )
        .eq(pd.Timestamp(DATE))
        .rename("result")
    )
    backend.assert_series_equal(result, expected)
