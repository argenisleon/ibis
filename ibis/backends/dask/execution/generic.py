"""Execution rules for generic ibis operations."""

import collections
import datetime
import decimal
import numbers

import dask.array as da
import dask.dataframe as dd
import dask.dataframe.groupby as ddgb
import numpy as np
import pandas as pd
from pandas import isnull, to_datetime
from pandas.api.types import DatetimeTZDtype

import ibis.common.exceptions as com
import ibis.expr.datatypes as dt
import ibis.expr.operations as ops
import ibis.expr.types as ir
from ibis.backends.dask import Backend as DaskBackend
from ibis.backends.dask.client import DaskTable
from ibis.backends.dask.core import execute
from ibis.backends.dask.dispatch import execute_node
from ibis.backends.dask.execution.util import (
    TypeRegistrationDict,
    make_selected_obj,
    register_types_to_dispatcher,
)
from ibis.backends.pandas.core import (
    date_types,
    integer_types,
    numeric_types,
    simple_types,
    timestamp_types,
)
from ibis.backends.pandas.execution import constants
from ibis.backends.pandas.execution.generic import (
    _execute_binary_op_impl,
    execute_between,
    execute_cast_series_array,
    execute_cast_series_generic,
    execute_count_star_frame,
    execute_count_star_frame_filter,
    execute_count_star_frame_groupby,
    execute_database_table_client,
    execute_difference_dataframe_dataframe,
    execute_distinct_dataframe,
    execute_intersection_dataframe_dataframe,
    execute_isinf,
    execute_isnan,
    execute_node_contains_series_sequence,
    execute_node_dropna_dataframe,
    execute_node_fillna_dataframe_dict,
    execute_node_fillna_dataframe_scalar,
    execute_node_ifnull_series,
    execute_node_not_contains_series_sequence,
    execute_node_nullif_series,
    execute_node_nullif_series_scalar,
    execute_node_self_reference_dataframe,
    execute_null_if_zero_series,
    execute_searched_case,
    execute_series_clip,
    execute_series_isnull,
    execute_series_notnnull,
    execute_sort_key_series,
    execute_table_column_df_or_df_groupby,
    execute_zero_if_null_series,
)

# Many dask and pandas functions are functionally equivalent, so we just add
# on registrations for dask types
DASK_DISPATCH_TYPES: TypeRegistrationDict = {
    ops.Cast: [
        ((dd.Series, dt.DataType), execute_cast_series_generic),
        ((dd.Series, dt.Array), execute_cast_series_array),
    ],
    ops.SortKey: [((dd.Series, bool), execute_sort_key_series)],
    ops.Clip: [
        (
            (
                dd.Series,
                (dd.Series, type(None)) + numeric_types,
                (dd.Series, type(None)) + numeric_types,
            ),
            execute_series_clip,
        ),
    ],
    ops.TableColumn: [
        (
            ((dd.DataFrame, ddgb.DataFrameGroupBy),),
            execute_table_column_df_or_df_groupby,
        ),
    ],
    ops.CountStar: [
        (
            (ddgb.DataFrameGroupBy, type(None)),
            execute_count_star_frame_groupby,
        ),
        ((dd.DataFrame, type(None)), execute_count_star_frame),
        ((dd.DataFrame, dd.Series), execute_count_star_frame_filter),
    ],
    ops.NullIfZero: [((dd.Series,), execute_null_if_zero_series)],
    ops.Between: [
        (
            (
                dd.Series,
                (dd.Series, numbers.Real, str, datetime.datetime),
                (dd.Series, numbers.Real, str, datetime.datetime),
            ),
            execute_between,
        ),
    ],
    ops.Intersection: [
        (
            (dd.DataFrame, dd.DataFrame, bool),
            execute_intersection_dataframe_dataframe,
        )
    ],
    ops.Difference: [
        (
            (dd.DataFrame, dd.DataFrame, bool),
            execute_difference_dataframe_dataframe,
        )
    ],
    ops.DropNa: [((dd.DataFrame,), execute_node_dropna_dataframe)],
    ops.FillNa: [
        ((dd.DataFrame, simple_types), execute_node_fillna_dataframe_scalar),
        ((dd.DataFrame,), execute_node_fillna_dataframe_dict),
    ],
    ops.IsNull: [((dd.Series,), execute_series_isnull)],
    ops.NotNull: [((dd.Series,), execute_series_notnnull)],
    ops.IsNan: [((dd.Series,), execute_isnan)],
    ops.IsInf: [((dd.Series,), execute_isinf)],
    ops.SelfReference: [
        ((dd.DataFrame,), execute_node_self_reference_dataframe)
    ],
    ops.Contains: [
        (
            (
                dd.Series,
                (collections.abc.Sequence, collections.abc.Set, dd.Series),
            ),
            execute_node_contains_series_sequence,
        )
    ],
    ops.NotContains: [
        (
            (
                dd.Series,
                (collections.abc.Sequence, collections.abc.Set, dd.Series),
            ),
            execute_node_not_contains_series_sequence,
        )
    ],
    ops.IfNull: [
        ((dd.Series, simple_types), execute_node_ifnull_series),
        ((dd.Series, dd.Series), execute_node_ifnull_series),
    ],
    ops.NullIf: [
        ((dd.Series, dd.Series), execute_node_nullif_series),
        ((dd.Series, simple_types), execute_node_nullif_series_scalar),
    ],
    ops.Distinct: [((dd.DataFrame,), execute_distinct_dataframe)],
    ops.ZeroIfNull: [
        ((dd.Series,), execute_zero_if_null_series),
        (
            (type(None), type(pd.NA), np.floating, float),
            execute_zero_if_null_series,
        ),
    ],
}

register_types_to_dispatcher(execute_node, DASK_DISPATCH_TYPES)

execute_node.register(DaskTable, DaskBackend)(execute_database_table_client)


@execute_node.register(ops.NodeList, collections.abc.Sequence)
def execute_node_value_list(op, _, **kwargs):
    return [execute(arg, **kwargs) for arg in op.values]


@execute_node.register(ops.Alias, object)
def execute_alias_series(op, _, **kwargs):
    # just compile the underlying argument because the naming is handled
    # by the translator for the top level expression
    return execute(op.arg, **kwargs)


@execute_node.register(ops.Arbitrary, dd.Series, (dd.Series, type(None)))
def execute_arbitrary_series_mask(op, data, mask, aggcontext=None, **kwargs):
    """
    Note: we cannot use the pandas version because Dask does not support .iloc
    See https://docs.dask.org/en/latest/dataframe-indexing.html. .loc will
    only work if our index lines up with the label.
    """
    data = data[mask] if mask is not None else data
    if op.how == 'first':
        index = 0
    elif op.how == 'last':
        index = len(data) - 1  # TODO - computation
    else:
        raise com.OperationNotDefinedError(
            f'Arbitrary {op.how!r} is not supported'
        )

    return data.loc[index]


@execute_node.register(ops.Arbitrary, ddgb.SeriesGroupBy, type(None))
def execute_arbitrary_series_groupby(op, data, _, aggcontext=None, **kwargs):
    how = op.how
    if how is None:
        how = 'first'

    if how not in {'first', 'last'}:
        raise com.OperationNotDefinedError(
            f'Arbitrary {how!r} is not supported'
        )
    return aggcontext.agg(data, how)


@execute_node.register(ops.Cast, ddgb.SeriesGroupBy, dt.DataType)
def execute_cast_series_group_by(op, data, type, **kwargs):
    result = execute_cast_series_generic(
        op, make_selected_obj(data), type, **kwargs
    )
    return result.groupby(data.index)


@execute_node.register(ops.Cast, dd.Series, dt.Timestamp)
def execute_cast_series_timestamp(op, data, type, **kwargs):
    arg = op.arg
    from_type = arg.output_dtype

    if from_type.equals(type):  # noop cast
        return data

    tz = type.timezone

    if isinstance(from_type, (dt.Timestamp, dt.Date)):
        return data.astype(
            'M8[ns]' if tz is None else DatetimeTZDtype('ns', tz)
        )

    if isinstance(from_type, (dt.String, dt.Integer)):
        timestamps = data.map_partitions(
            to_datetime,
            infer_datetime_format=True,
            meta=(data.name, 'datetime64[ns]'),
        )
        # TODO - is there a better way to do this
        timestamps = timestamps.astype(timestamps.head(1).dtype)
        if getattr(timestamps.dtype, "tz", None) is not None:
            return timestamps.dt.tz_convert(tz)
        else:
            return timestamps.dt.tz_localize(tz)

    raise TypeError(f"Don't know how to cast {from_type} to {type}")


@execute_node.register(ops.Cast, dd.Series, dt.Date)
def execute_cast_series_date(op, data, type, **kwargs):
    arg = op.args[0]
    from_type = arg.output_dtype

    if from_type.equals(type):
        return data

    # TODO - we return slightly different things depending on the branch
    # double check what the logic should be

    if isinstance(from_type, dt.Timestamp):
        return data.dt.normalize()

    if from_type.equals(dt.string):
        # TODO - this is broken
        datetimes = data.map_partitions(
            to_datetime,
            infer_datetime_format=True,
            meta=(data.name, 'datetime64[ns]'),
        )

        # TODO - we are getting rid of the index here
        return datetimes.dt.normalize()

    if isinstance(from_type, dt.Integer):
        return data.map_partitions(
            to_datetime, unit='D', meta=(data.name, 'datetime64[ns]')
        )

    raise TypeError(f"Don't know how to cast {from_type} to {type}")


@execute_node.register(ops.Limit, dd.DataFrame, integer_types, integer_types)
def execute_limit_frame(op, data, nrows, offset, **kwargs):
    # NOTE: Dask Dataframes do not support iloc row based indexing
    return data.loc[offset : (offset + nrows) - 1]


@execute_node.register(ops.Not, (dd.core.Scalar, dd.Series))
def execute_not_scalar_or_series(op, data, **kwargs):
    return ~data


@execute_node.register(ops.Binary, dd.Series, dd.Series)
@execute_node.register(ops.Binary, dd.Series, dd.core.Scalar)
@execute_node.register(ops.Binary, dd.core.Scalar, dd.Series)
@execute_node.register(
    (ops.NumericBinary, ops.LogicalBinary, ops.Comparison),
    numeric_types,
    dd.Series,
)
@execute_node.register(
    (ops.NumericBinary, ops.LogicalBinary, ops.Comparison),
    dd.Series,
    numeric_types,
)
@execute_node.register((ops.Comparison, ops.Add, ops.Multiply), dd.Series, str)
@execute_node.register((ops.Comparison, ops.Add, ops.Multiply), str, dd.Series)
@execute_node.register(ops.Comparison, dd.Series, timestamp_types)
@execute_node.register(ops.Comparison, timestamp_types, dd.Series)
def execute_binary_op(op, left, right, **kwargs):
    return _execute_binary_op_impl(op, left, right, **kwargs)


@execute_node.register(ops.Comparison, dd.Series, date_types)
def execute_binary_op_date_right(op, left, right, **kwargs):
    return _execute_binary_op_impl(
        op, dd.to_datetime(left), pd.to_datetime(right), **kwargs
    )


@execute_node.register(ops.Binary, ddgb.SeriesGroupBy, ddgb.SeriesGroupBy)
def execute_binary_op_series_group_by(op, left, right, **kwargs):
    if left.index != right.index:
        raise ValueError(
            'Cannot perform {} operation on two series with '
            'different groupings'.format(type(op).__name__)
        )
    result = execute_binary_op(
        op, make_selected_obj(left), make_selected_obj(right), **kwargs
    )
    return result.groupby(left.index)


@execute_node.register(ops.Binary, ddgb.SeriesGroupBy, simple_types)
def execute_binary_op_series_gb_simple(op, left, right, **kwargs):
    result = execute_binary_op(op, make_selected_obj(left), right, **kwargs)
    return result.groupby(left.index)


@execute_node.register(ops.Binary, simple_types, ddgb.SeriesGroupBy)
def execute_binary_op_simple_series_gb(op, left, right, **kwargs):
    result = execute_binary_op(op, left, make_selected_obj(right), **kwargs)
    return result.groupby(right.index)


@execute_node.register(ops.Unary, ddgb.SeriesGroupBy)
def execute_unary_op_series_gb(op, operand, **kwargs):
    result = execute_node(op, make_selected_obj(operand), **kwargs)
    return result.groupby(operand.index)


@execute_node.register(
    (ops.Log, ops.Round),
    ddgb.SeriesGroupBy,
    (numbers.Real, decimal.Decimal, type(None)),
)
def execute_log_series_gb_others(op, left, right, **kwargs):
    result = execute_node(op, make_selected_obj(left), right, **kwargs)
    return result.groupby(left.index)


@execute_node.register(
    (ops.Log, ops.Round), ddgb.SeriesGroupBy, ddgb.SeriesGroupBy
)
def execute_log_series_gb_series_gb(op, left, right, **kwargs):
    result = execute_node(
        op, make_selected_obj(left), make_selected_obj(right), **kwargs
    )
    return result.groupby(left.index)


@execute_node.register(ops.Union, dd.DataFrame, dd.DataFrame, bool)
def execute_union_dataframe_dataframe(
    op, left: dd.DataFrame, right: dd.DataFrame, distinct, **kwargs
):
    result = dd.concat([left, right], axis=0)
    return result.drop_duplicates() if distinct else result


@execute_node.register(ops.IfNull, simple_types, dd.Series)
def execute_node_ifnull_scalar_series(op, value, replacement, **kwargs):
    return (
        replacement
        if isnull(value)
        else dd.from_pandas(
            pd.Series(value, index=replacement.index),
            npartitions=replacement.npartitions,
        )
    )


@execute_node.register(ops.NullIf, simple_types, dd.Series)
def execute_node_nullif_scalar_series(op, value, series, **kwargs):
    # TODO - not preserving the index
    return dd.from_array(da.where(series.eq(value).values, np.nan, value))


def wrap_case_result(raw: np.ndarray, expr: ir.Value):
    """Wrap a CASE statement result in a Series and handle returning scalars.

    Parameters
    ----------
    raw : ndarray[T]
        The raw results of executing the ``CASE`` expression
    expr : Value
        The expression from the which `raw` was computed

    Returns
    -------
    Union[scalar, Series]
    """
    raw_1d = np.atleast_1d(raw)
    if np.any(isnull(raw_1d)):
        result = dd.from_array(raw_1d)
    else:
        result = dd.from_array(
            raw_1d.astype(constants.IBIS_TYPE_TO_PANDAS_TYPE[expr.type()])
        )
    # TODO - we force computation here
    if isinstance(expr, ir.Scalar) and result.size.compute() == 1:
        return result.head().item()
    return result


@execute_node.register(ops.SearchedCase, list, list, object)
def execute_searched_case_dask(op, whens, thens, otherwise, **kwargs):
    if not isinstance(whens[0], dd.Series):
        # if we are not dealing with dask specific objects, fallback to the
        # pandas logic. For example, in the case of ibis literals.
        # See `test_functions/test_ifelse_returning_bool` or
        # `test_operations/test_searched_case_scalar` for code that hits this.
        return execute_searched_case(op, whens, thens, otherwise, **kwargs)
    if otherwise is None:
        otherwise = np.nan
    idx = whens[0].index
    whens = [w.to_dask_array() for w in whens]
    if isinstance(thens[0], dd.Series):
        # some computed column
        thens = [t.to_dask_array() for t in thens]
    else:
        # scalar
        thens = [da.from_array(np.array([t])) for t in thens]
    raw = da.select(whens, thens, otherwise)
    out = dd.from_dask_array(
        raw,
        index=idx,
    )
    return out


@execute_node.register(ops.SimpleCase, dd.Series, list, list, object)
def execute_simple_case_series(op, value, whens, thens, otherwise, **kwargs):
    if otherwise is None:
        otherwise = np.nan
    raw = np.select([value == when for when in whens], thens, otherwise)
    return wrap_case_result(raw, op.to_expr())
