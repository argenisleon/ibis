# Copyright 2015 Cloudera Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


import os

import numpy as np
import pandas as pd
import pytest
from pytest import param

import ibis
import ibis.expr.datatypes as dt
import ibis.expr.types as ir
from ibis.tests.util import assert_equal

pytest.importorskip("psycopg2")
sa = pytest.importorskip("sqlalchemy")

from sqlalchemy.dialects import postgresql  # noqa: E402

from ibis.backends.base.sql.alchemy import schema_from_table  # noqa: E402

POSTGRES_TEST_DB = os.environ.get(
    'IBIS_TEST_POSTGRES_DATABASE', 'ibis_testing'
)
IBIS_POSTGRES_HOST = os.environ.get('IBIS_TEST_POSTGRES_HOST', 'localhost')
IBIS_POSTGRES_PORT = os.environ.get('IBIS_TEST_POSTGRES_PORT', '5432')
IBIS_POSTGRES_USER = os.environ.get('IBIS_TEST_POSTGRES_USER', 'postgres')
IBIS_POSTGRES_PASS = os.environ.get('IBIS_TEST_POSTGRES_PASSWORD', 'postgres')


def test_table(alltypes):
    assert isinstance(alltypes, ir.Table)


def test_array_execute(alltypes):
    d = alltypes.limit(10).double_col
    s = d.execute()
    assert isinstance(s, pd.Series)
    assert len(s) == 10


def test_literal_execute(con):
    expr = ibis.literal('1234')
    result = con.execute(expr)
    assert result == '1234'


def test_simple_aggregate_execute(alltypes):
    d = alltypes.double_col.sum()
    v = d.execute()
    assert isinstance(v, float)


def test_list_tables(con):
    assert len(con.list_tables()) > 0
    assert len(con.list_tables(like='functional')) == 1


def test_compile_toplevel():
    t = ibis.table([('foo', 'double')], name='t0')

    # it works!
    expr = t.foo.sum()
    result = ibis.postgres.compile(expr)
    expected = "SELECT sum(t0.foo) AS sum \nFROM t0 AS t0"

    assert str(result) == expected


def test_list_databases(con):
    assert POSTGRES_TEST_DB is not None
    assert POSTGRES_TEST_DB in con.list_databases()


def test_metadata_is_per_table():
    con = ibis.postgres.connect(
        host=IBIS_POSTGRES_HOST,
        database=POSTGRES_TEST_DB,
        user=IBIS_POSTGRES_USER,
        password=IBIS_POSTGRES_PASS,
        port=IBIS_POSTGRES_PORT,
    )
    assert len(con.meta.tables) == 0

    # assert that we reflect only when a table is requested
    con.table('functional_alltypes')
    assert 'functional_alltypes' in con.meta.tables
    assert len(con.meta.tables) == 1


def test_schema_type_conversion():
    typespec = [
        # name, type, nullable
        ('json', postgresql.JSON, True, dt.JSON),
        ('jsonb', postgresql.JSONB, True, dt.JSONB),
        ('uuid', postgresql.UUID, True, dt.UUID),
        ('macaddr', postgresql.MACADDR, True, dt.MACADDR),
        ('inet', postgresql.INET, True, dt.INET),
    ]

    sqla_types = []
    ibis_types = []
    for name, t, nullable, ibis_type in typespec:
        sqla_type = sa.Column(name, t, nullable=nullable)
        sqla_types.append(sqla_type)
        ibis_types.append((name, ibis_type(nullable=nullable)))

    # Create a table with placeholder stubs for JSON, JSONB, and UUID.
    engine = sa.create_engine('postgresql://')
    table = sa.Table('tname', sa.MetaData(bind=engine), *sqla_types)

    # Check that we can correctly create a schema with dt.any for the
    # missing types.
    schema = schema_from_table(table)
    expected = ibis.schema(ibis_types)

    assert_equal(schema, expected)


def test_interval_films_schema(con):
    t = con.table("films")
    assert t.len.type() == dt.Interval(unit="m")
    assert t.len.execute().dtype == np.dtype("timedelta64[ns]")


@pytest.mark.parametrize(
    ("column", "expected_dtype"),
    [
        # a, b and g are variable length intervals, like YEAR TO MONTH
        ("c", dt.Interval("D")),
        ("d", dt.Interval("h")),
        ("e", dt.Interval("m")),
        ("f", dt.Interval("s")),
        ("h", dt.Interval("h")),
        ("i", dt.Interval("m")),
        ("j", dt.Interval("s")),
        ("k", dt.Interval("m")),
        ("l", dt.Interval("s")),
        ("m", dt.Interval("s")),
    ],
)
def test_all_interval_types_schema(intervals, column, expected_dtype):
    assert intervals[column].type() == expected_dtype


@pytest.mark.parametrize(
    ("column", "expected_dtype"),
    [
        # a, b and g are variable length intervals, like YEAR TO MONTH
        ("c", dt.Interval("D")),
        ("d", dt.Interval("h")),
        ("e", dt.Interval("m")),
        ("f", dt.Interval("s")),
        ("h", dt.Interval("h")),
        ("i", dt.Interval("m")),
        ("j", dt.Interval("s")),
        ("k", dt.Interval("m")),
        ("l", dt.Interval("s")),
        ("m", dt.Interval("s")),
    ],
)
def test_all_interval_types_execute(intervals, column, expected_dtype):
    expr = intervals[column]
    series = expr.execute()
    assert series.dtype == np.dtype("timedelta64[ns]")


@pytest.mark.xfail(
    raises=ValueError, reason="Year and month interval types not yet supported"
)
def test_unsupported_intervals(con):
    t = con.table("not_supported_intervals")
    assert t["a"].type() == dt.Interval("Y")
    assert t["b"].type() == dt.Interval("M")
    assert t["g"].type() == dt.Interval("M")


@pytest.mark.parametrize('params', [{}, {'database': POSTGRES_TEST_DB}])
def test_create_and_drop_table(con, temp_table, params):
    sch = ibis.schema(
        [
            ('first_name', 'string'),
            ('last_name', 'string'),
            ('department_name', 'string'),
            ('salary', 'float64'),
        ]
    )

    con.create_table(temp_table, schema=sch, **params)
    assert con.table(temp_table, **params) is not None

    con.drop_table(temp_table, **params)

    with pytest.raises(sa.exc.NoSuchTableError):
        con.table(temp_table, **params)


@pytest.mark.parametrize(
    ("pg_type", "expected_type"),
    [
        param(pg_type, ibis_type, id=pg_type.lower())
        for (pg_type, ibis_type) in [
            ("boolean", dt.boolean),
            ("bytea", dt.binary),
            ("char", dt.string),
            ("bigint", dt.int64),
            ("smallint", dt.int16),
            ("integer", dt.int32),
            ("text", dt.string),
            ("json", dt.json),
            ("point", dt.point),
            ("polygon", dt.polygon),
            ("line", dt.linestring),
            ("real", dt.float32),
            ("double precision", dt.float64),
            ("macaddr", dt.macaddr),
            ("macaddr8", dt.macaddr),
            ("inet", dt.inet),
            ("character", dt.string),
            ("character varying", dt.string),
            ("date", dt.date),
            ("time", dt.time),
            ("time without time zone", dt.time),
            ("timestamp without time zone", dt.timestamp),
            ("timestamp with time zone", dt.Timestamp("UTC")),
            ("interval", dt.interval),
            ("numeric", dt.decimal),
            ("numeric(3, 2)", dt.Decimal(3, 2)),
            ("uuid", dt.uuid),
            ("jsonb", dt.jsonb),
            ("geometry", dt.geometry),
            ("geography", dt.geography),
        ]
    ],
)
def test_get_schema_from_query(con, pg_type, expected_type):
    raw_name = ibis.util.guid()
    name = con.con.dialect.identifier_preparer.quote_identifier(raw_name)
    con.raw_sql(f"CREATE TEMPORARY TABLE {name} (x {pg_type}, y {pg_type}[])")
    expected_schema = ibis.schema(
        dict(x=expected_type, y=dt.Array(expected_type))
    )
    result_schema = con._get_schema_using_query(f"SELECT x, y FROM {name}")
    assert result_schema == expected_schema
