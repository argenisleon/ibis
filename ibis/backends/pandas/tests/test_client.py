import numpy as np
import pandas as pd
import pandas.testing as tm
import pytest
from pytest import param

import ibis
from ibis.backends.pandas.client import PandasTable  # noqa: E402


@pytest.fixture
def client():
    return ibis.pandas.connect(
        {
            'df': pd.DataFrame({'a': [1, 2, 3], 'b': list('abc')}),
            'df_unknown': pd.DataFrame(
                {'array_of_strings': [['a', 'b'], [], ['c']]}
            ),
        }
    )


@pytest.fixture
def table(client):
    return client.table('df')


@pytest.fixture
def test_data():
    test_data = test_data = pd.DataFrame(
        {"A": [1, 2, 3, 4, 5], "B": list("abcde")}
    )
    return test_data


def test_client_table(table):
    assert isinstance(table.op(), ibis.expr.operations.DatabaseTable)
    assert isinstance(table.op(), PandasTable)


def test_client_table_repr(table):
    assert 'PandasTable' in repr(table)


def test_load_data(client, test_data):
    client.load_data('testing', test_data)
    assert 'testing' in client.list_tables()
    assert client.get_schema('testing')


def test_create_table(client, test_data):
    client.create_table('testing', obj=test_data)
    assert 'testing' in client.list_tables()
    client.create_table('testingschema', schema=client.get_schema('testing'))
    assert 'testingschema' in client.list_tables()


def test_literal(client):
    lit = ibis.literal(1)
    result = client.execute(lit)
    assert result == 1


def test_list_tables(client):
    assert client.list_tables(like='df_unknown')
    assert not client.list_tables(like='not_in_the_database')
    assert client.list_tables()


def test_drop(table):
    table = table.mutate(c=table.a)
    expr = table.drop('a')
    result = expr.execute()
    expected = table[['b', 'c']].execute()
    tm.assert_frame_equal(result, expected)


@pytest.mark.parametrize(
    'unit',
    [
        param('Y', marks=pytest.mark.xfail(raises=TypeError)),
        param('M', marks=pytest.mark.xfail(raises=TypeError)),
        param('D', marks=pytest.mark.xfail(raises=TypeError)),
        param('h', marks=pytest.mark.xfail(raises=TypeError)),
        param('m', marks=pytest.mark.xfail(raises=TypeError)),
        param('s', marks=pytest.mark.xfail(raises=TypeError)),
        param('ms', marks=pytest.mark.xfail(raises=TypeError)),
        param('us', marks=pytest.mark.xfail(raises=TypeError)),
        'ns',
        param('ps', marks=pytest.mark.xfail(raises=TypeError)),
        param('fs', marks=pytest.mark.xfail(raises=TypeError)),
        param('as', marks=pytest.mark.xfail(raises=TypeError)),
    ],
)
def test_datetime64_infer(client, unit):
    value = np.datetime64('2018-01-02', unit)
    expr = ibis.literal(value, type='timestamp')
    result = client.execute(expr)
    assert result == value
