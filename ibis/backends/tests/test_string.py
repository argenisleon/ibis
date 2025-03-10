import pytest
from pytest import param

import ibis
import ibis.expr.datatypes as dt


def is_text_type(x):
    return isinstance(x, str)


def test_string_col_is_unicode(alltypes, df):
    dtype = alltypes.string_col.type()
    assert dtype == dt.String(nullable=dtype.nullable)
    assert df.string_col.map(is_text_type).all()
    result = alltypes.string_col.execute()
    assert result.map(is_text_type).all()


@pytest.mark.parametrize(
    ('result_func', 'expected_func'),
    [
        param(
            lambda t: t.string_col.contains('6'),
            lambda t: t.string_col.str.contains('6'),
            id='contains',
            marks=pytest.mark.notimpl(["datafusion"]),
        ),
        param(
            lambda t: t.string_col.like('6%'),
            lambda t: t.string_col.str.contains('6.*'),
            id='like',
            marks=pytest.mark.notimpl(["datafusion"]),
        ),
        param(
            lambda t: t.string_col.like('6^%'),
            lambda t: t.string_col.str.contains('6%'),
            id='complex_like_escape',
            marks=pytest.mark.notimpl(["datafusion"]),
        ),
        param(
            lambda t: t.string_col.like('6^%%'),
            lambda t: t.string_col.str.contains('6%.*'),
            id='complex_like_escape_match',
            marks=pytest.mark.notimpl(["datafusion"]),
        ),
        param(
            lambda t: t.string_col.ilike('6%'),
            lambda t: t.string_col.str.contains('6.*'),
            id='ilike',
            marks=pytest.mark.notimpl(["datafusion", "impala", "pyspark"]),
        ),
        param(
            lambda t: t.string_col.re_search(r'[[:digit:]]+'),
            lambda t: t.string_col.str.contains(r'\d+'),
            id='re_search_posix',
            marks=pytest.mark.notimpl(["datafusion", "pyspark", "snowflake"]),
        ),
        param(
            lambda t: t.string_col.re_extract(r'([[:digit:]]+)', 0),
            lambda t: t.string_col.str.extract(r'(\d+)', expand=False),
            id='re_extract_posix',
            marks=pytest.mark.notimpl(["mysql", "pyspark", "snowflake"]),
        ),
        param(
            lambda t: t.string_col.re_replace(r'[[:digit:]]+', 'a'),
            lambda t: t.string_col.str.replace(r'\d+', 'a', regex=True),
            id='re_replace_posix',
            marks=pytest.mark.notimpl(
                ['datafusion', "mysql", "pyspark", "snowflake"]
            ),
        ),
        param(
            lambda t: t.string_col.re_search(r'\d+'),
            lambda t: t.string_col.str.contains(r'\d+'),
            id='re_search',
            marks=pytest.mark.notimpl(["impala", "datafusion", "snowflake"]),
        ),
        param(
            lambda t: t.string_col.re_extract(r'(\d+)', 0),
            lambda t: t.string_col.str.extract(r'(\d+)', expand=False),
            id='re_extract',
            marks=pytest.mark.notimpl(["impala", "mysql", "snowflake"]),
        ),
        param(
            lambda t: t.string_col.re_replace(r'\d+', 'a'),
            lambda t: t.string_col.str.replace(r'\d+', 'a', regex=True),
            id='re_replace',
            marks=pytest.mark.notimpl(
                ["impala", "datafusion", "mysql", "snowflake"]
            ),
        ),
        param(
            lambda t: t.string_col.repeat(2),
            lambda t: t.string_col * 2,
            id="repeat_method",
        ),
        param(
            lambda t: 2 * t.string_col,
            lambda t: 2 * t.string_col,
            id="repeat_left",
        ),
        param(
            lambda t: t.string_col * 2,
            lambda t: t.string_col * 2,
            id="repeat_right",
        ),
        param(
            lambda t: t.string_col.translate('0', 'a'),
            lambda t: t.string_col.str.translate(str.maketrans('0', 'a')),
            id='translate',
            marks=pytest.mark.notimpl(["clickhouse", "datafusion", "mysql"]),
        ),
        param(
            lambda t: t.string_col.find('a'),
            lambda t: t.string_col.str.find('a'),
            id='find',
            marks=pytest.mark.notimpl(["datafusion"]),
        ),
        param(
            lambda t: t.string_col.lpad(10, 'a'),
            lambda t: t.string_col.str.pad(10, fillchar='a', side='left'),
            id='lpad',
        ),
        param(
            lambda t: t.string_col.rpad(10, 'a'),
            lambda t: t.string_col.str.pad(10, fillchar='a', side='right'),
            id='rpad',
        ),
        param(
            lambda t: t.string_col.find_in_set(['1']),
            lambda t: t.string_col.str.find('1'),
            id='find_in_set',
            marks=pytest.mark.notimpl(
                ["datafusion", "pyspark", "sqlite", "snowflake"]
            ),
        ),
        param(
            lambda t: t.string_col.find_in_set(['a']),
            lambda t: t.string_col.str.find('a'),
            id='find_in_set_all_missing',
            marks=pytest.mark.notimpl(
                ["datafusion", "pyspark", "sqlite", "snowflake"]
            ),
        ),
        param(
            lambda t: t.string_col.lower(),
            lambda t: t.string_col.str.lower(),
            id='lower',
        ),
        param(
            lambda t: t.string_col.upper(),
            lambda t: t.string_col.str.upper(),
            id='upper',
        ),
        param(
            lambda t: t.string_col.reverse(),
            lambda t: t.string_col.str[::-1],
            id='reverse',
        ),
        param(
            lambda t: t.string_col.ascii_str(),
            lambda t: t.string_col.map(ord).astype('int32'),
            id='ascii_str',
            # TODO(dask) - dtype - #2553
            marks=pytest.mark.notimpl(["clickhouse", "dask", "datafusion"]),
        ),
        param(
            lambda t: t.string_col.length(),
            lambda t: t.string_col.str.len().astype('int32'),
            id='length',
        ),
        param(
            lambda t: t.string_col.startswith('foo'),
            lambda t: t.string_col.str.startswith('foo'),
            id='startswith',
            marks=pytest.mark.notimpl(["dask", "datafusion", "pandas"]),
        ),
        param(
            lambda t: t.string_col.endswith('foo'),
            lambda t: t.string_col.str.endswith('foo'),
            id='endswith',
            marks=pytest.mark.notimpl(["dask", "datafusion", "pandas"]),
        ),
        param(
            lambda t: t.string_col.strip(),
            lambda t: t.string_col.str.strip(),
            id='strip',
        ),
        param(
            lambda t: t.string_col.lstrip(),
            lambda t: t.string_col.str.lstrip(),
            id='lstrip',
        ),
        param(
            lambda t: t.string_col.rstrip(),
            lambda t: t.string_col.str.rstrip(),
            id='rstrip',
        ),
        param(
            lambda t: t.string_col.capitalize(),
            lambda t: t.string_col.str.capitalize(),
            id='capitalize',
            marks=pytest.mark.notimpl(["clickhouse", "duckdb"]),
        ),
        param(
            lambda t: t.date_string_col.substr(2, 3),
            lambda t: t.date_string_col.str[2:5],
            id='substr',
        ),
        param(
            lambda t: t.date_string_col.left(2),
            lambda t: t.date_string_col.str[:2],
            id='left',
        ),
        param(
            lambda t: t.date_string_col.right(2),
            lambda t: t.date_string_col.str[-2:],
            id="right",
            marks=pytest.mark.notimpl(["datafusion"]),
        ),
        param(
            lambda t: t.date_string_col[1:3],
            lambda t: t.date_string_col.str[1:3],
            id='slice',
        ),
        param(
            lambda t: t.date_string_col[t.date_string_col.length() - 1 :],
            lambda t: t.date_string_col.str[-1:],
            id='expr_slice_begin',
            # TODO: substring #2553
            marks=pytest.mark.notimpl(["dask", "pyspark"]),
        ),
        param(
            lambda t: t.date_string_col[: t.date_string_col.length()],
            lambda t: t.date_string_col,
            id='expr_slice_end',
            # TODO: substring #2553
            marks=pytest.mark.notimpl(["dask", "pyspark"]),
        ),
        param(
            lambda t: t.date_string_col[:],
            lambda t: t.date_string_col,
            id='expr_empty_slice',
            # TODO: substring #2553
            marks=pytest.mark.notimpl(["dask", "pyspark"]),
        ),
        param(
            lambda t: t.date_string_col[
                t.date_string_col.length() - 2 : t.date_string_col.length() - 1
            ],
            lambda t: t.date_string_col.str[-2:-1],
            id='expr_slice_begin_end',
            # TODO: substring #2553
            marks=pytest.mark.notimpl(["dask", "pyspark"]),
        ),
        param(
            lambda t: t.date_string_col.split('/'),
            lambda t: t.date_string_col.str.split('/'),
            id='split',
            marks=pytest.mark.notimpl(
                [
                    "dask",
                    "datafusion",
                    "impala",
                    "mysql",
                    "sqlite",
                    "snowflake",
                ]
            ),
        ),
        param(
            lambda t: ibis.literal('-').join(['a', t.string_col, 'c']),
            lambda t: 'a-' + t.string_col + '-c',
            id='join',
            marks=pytest.mark.notimpl(["datafusion"]),
        ),
        param(
            lambda t: t.string_col + t.date_string_col,
            lambda t: t.string_col + t.date_string_col,
            id='concat_columns',
            marks=pytest.mark.notimpl(["datafusion"]),
        ),
        param(
            lambda t: t.string_col + 'a',
            lambda t: t.string_col + 'a',
            id='concat_column_scalar',
            marks=pytest.mark.notimpl(["datafusion"]),
        ),
        param(
            lambda t: 'a' + t.string_col,
            lambda t: 'a' + t.string_col,
            id='concat_scalar_column',
            marks=pytest.mark.notimpl(["datafusion"]),
        ),
        param(
            lambda t: t.string_col.replace("1", "42"),
            lambda t: t.string_col.str.replace("1", "42"),
            id="replace",
            marks=pytest.mark.notimpl(["datafusion"]),
        ),
    ],
)
def test_string(backend, alltypes, df, result_func, expected_func):
    expr = result_func(alltypes).name('tmp')
    result = expr.execute()

    expected = backend.default_series_rename(expected_func(df))
    backend.assert_series_equal(result, expected)


@pytest.mark.notimpl(["datafusion"])
def test_substr_with_null_values(backend, alltypes, df):
    table = alltypes.mutate(
        substr_col_null=ibis.case()
        .when(alltypes['bool_col'], alltypes['string_col'])
        .else_(None)
        .end()
        .substr(0, 2)
    )
    result = table.execute()

    expected = df.copy()
    mask = ~expected['bool_col']
    expected['substr_col_null'] = expected['string_col']
    expected.loc[mask, 'substr_col_null'] = None
    expected['substr_col_null'] = expected['substr_col_null'].str.slice(0, 2)

    backend.assert_frame_equal(result, expected)
