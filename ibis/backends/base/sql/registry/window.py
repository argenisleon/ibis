from operator import add, mul, sub
from typing import Optional, Union

import ibis
import ibis.common.exceptions as com
import ibis.expr.analysis as L
import ibis.expr.datatypes as dt
import ibis.expr.operations as ops
import ibis.expr.types as ir

_map_interval_to_microseconds = {
    'W': 604800000000,
    'D': 86400000000,
    'h': 3600000000,
    'm': 60000000,
    's': 1000000,
    'ms': 1000,
    'us': 1,
    'ns': 0.001,
}


_map_interval_op_to_op = {
    # Literal Intervals have two args, i.e.
    # Literal(1, Interval(value_type=int8, unit='D', nullable=True))
    # Parse both args and multipy 1 * _map_interval_to_microseconds['D']
    ops.Literal: mul,
    ops.IntervalMultiply: mul,
    ops.IntervalAdd: add,
    ops.IntervalSubtract: sub,
}


_cumulative_to_reduction = {
    ops.CumulativeSum: ops.Sum,
    ops.CumulativeMin: ops.Min,
    ops.CumulativeMax: ops.Max,
    ops.CumulativeMean: ops.Mean,
    ops.CumulativeAny: ops.Any,
    ops.CumulativeAll: ops.All,
}


def _replace_interval_with_scalar(expr: Union[ir.Expr, dt.Interval, float]):
    """Good old Depth-First Search to identify the Interval and IntervalValue
    components of the expression and return a comparable scalar expression.

    Parameters
    ----------
    expr : float or expression of intervals
        For example, ``ibis.interval(days=1) + ibis.interval(hours=5)``

    Returns
    -------
    preceding : float or ir.FloatingScalar, depending upon the expr
    """
    if isinstance(expr, ir.Expr):
        expr_op = expr.op()
    else:
        expr_op = None

    if not isinstance(expr, (dt.Interval, ir.IntervalValue)):
        # Literal expressions have op method but native types do not.
        if isinstance(expr_op, ops.Literal):
            return expr_op.value
        else:
            return expr
    elif isinstance(expr, dt.Interval):
        try:
            microseconds = _map_interval_to_microseconds[expr.unit]
            return microseconds
        except KeyError:
            raise ValueError(
                "Expected preceding values of week(), "
                + "day(), hour(), minute(), second(), millisecond(), "
                + f"microseconds(), nanoseconds(); got {expr}"
            )
    elif expr_op.args and isinstance(expr, ir.IntervalValue):
        if len(expr_op.args) > 2:
            raise NotImplementedError("'preceding' argument cannot be parsed.")
        left_arg = _replace_interval_with_scalar(expr_op.args[0])
        right_arg = _replace_interval_with_scalar(expr_op.args[1])
        method = _map_interval_op_to_op[type(expr_op)]
        return method(left_arg, right_arg)
    else:
        raise TypeError(f'expr has unknown type {type(expr).__name__}')


def cumulative_to_window(translator, op, window):
    klass = _cumulative_to_reduction[type(op)]
    new_op = klass(*op.args)

    try:
        rule = translator._rewrites[type(new_op)]
    except KeyError:
        pass
    else:
        new_op = rule(new_op)

    win = (
        ibis.cumulative_window()
        .group_by(window._group_by)
        .order_by(window._order_by)
    )
    new_expr = L.windowize_function(new_op.to_expr(), win)
    return new_expr.op()


def time_range_to_range_window(_, window):
    # Check that ORDER BY column is a single time column:
    order_by_vars = [x.op().args[0] for x in window._order_by]
    if len(order_by_vars) > 1:
        raise com.IbisInputError(
            f"Expected 1 order-by variable, got {len(order_by_vars)}"
        )

    order_var = window._order_by[0].op().args[0]
    timestamp_order_var = order_var.cast('int64')
    window = window._replace(order_by=timestamp_order_var, how='range')

    # Need to change preceding interval expression to scalars
    preceding = window.preceding
    if isinstance(preceding, ir.IntervalScalar):
        new_preceding = _replace_interval_with_scalar(preceding)
        window = window._replace(preceding=new_preceding)

    return window


def format_window(translator, op, window):
    components = []

    if window.max_lookback is not None:
        raise NotImplementedError(
            'Rows with max lookback is not implemented '
            'for Impala-based backends.'
        )

    if window._group_by:
        partition_args = ', '.join(map(translator.translate, window._group_by))
        components.append(f'PARTITION BY {partition_args}')

    if window._order_by:
        order_args = ', '.join(map(translator.translate, window._order_by))
        components.append(f'ORDER BY {order_args}')

    p, f = window.preceding, window.following

    def _prec(p: Optional[int]) -> str:
        assert p is None or p >= 0

        if p is None:
            prefix = 'UNBOUNDED'
        else:
            if not p:
                return 'CURRENT ROW'
            prefix = str(p)
        return f'{prefix} PRECEDING'

    def _foll(f: Optional[int]) -> str:
        assert f is None or f >= 0

        if f is None:
            prefix = 'UNBOUNDED'
        else:
            if not f:
                return 'CURRENT ROW'
            prefix = str(f)

        return f'{prefix} FOLLOWING'

    frame_clause_not_allowed = (
        ops.Lag,
        ops.Lead,
        ops.DenseRank,
        ops.MinRank,
        ops.NTile,
        ops.PercentRank,
        ops.CumeDist,
        ops.RowNumber,
    )

    if isinstance(op.expr, frame_clause_not_allowed):
        frame = None
    elif p is not None and f is not None:
        frame = '{} BETWEEN {} AND {}'.format(
            window.how.upper(), _prec(p), _foll(f)
        )

    elif p is not None:
        if isinstance(p, tuple):
            start, end = p
            frame = '{} BETWEEN {} AND {}'.format(
                window.how.upper(), _prec(start), _prec(end)
            )
        else:
            kind = 'ROWS' if p > 0 else 'RANGE'
            frame = '{} BETWEEN {} AND UNBOUNDED FOLLOWING'.format(
                kind, _prec(p)
            )
    elif f is not None:
        if isinstance(f, tuple):
            start, end = f
            frame = '{} BETWEEN {} AND {}'.format(
                window.how.upper(), _foll(start), _foll(end)
            )
        else:
            kind = 'ROWS' if f > 0 else 'RANGE'
            frame = '{} BETWEEN UNBOUNDED PRECEDING AND {}'.format(
                kind, _foll(f)
            )
    else:
        # no-op, default is full sample
        frame = None

    if frame is not None:
        components.append(frame)

    return 'OVER ({})'.format(' '.join(components))


_subtract_one = '({} - 1)'.format


_expr_transforms = {
    ops.RowNumber: _subtract_one,
    ops.DenseRank: _subtract_one,
    ops.MinRank: _subtract_one,
    ops.NTile: _subtract_one,
}


def window(translator, op):
    arg, window = op.args

    _require_order_by = (
        ops.Lag,
        ops.Lead,
        ops.DenseRank,
        ops.MinRank,
        ops.FirstValue,
        ops.LastValue,
        ops.PercentRank,
        ops.CumeDist,
        ops.NTile,
    )

    _unsupported_reductions = (
        ops.ApproxMedian,
        ops.GroupConcat,
        ops.ApproxCountDistinct,
    )

    if isinstance(arg, _unsupported_reductions):
        raise com.UnsupportedOperationError(
            f'{type(arg)} is not supported in window functions'
        )

    if isinstance(arg, ops.CumulativeOp):
        arg = cumulative_to_window(translator, arg, window)
        return translator.translate(arg)

    # Some analytic functions need to have the expression of interest in
    # the ORDER BY part of the window clause
    if isinstance(arg, _require_order_by) and not window._order_by:
        window = window.order_by(arg.args[0])

    # Time ranges need to be converted to microseconds.
    # FIXME(kszucs): avoid the expression roundtrip
    if window.how == 'range':
        order_by_types = [type(x.op().args[0]) for x in window._order_by]
        time_range_types = (ir.TimeColumn, ir.DateColumn, ir.TimestampColumn)
        if any(col_type in time_range_types for col_type in order_by_types):
            window = time_range_to_range_window(translator, window)

    window_formatted = format_window(translator, op, window)

    arg_formatted = translator.translate(arg)
    result = f'{arg_formatted} {window_formatted}'

    if type(arg) in _expr_transforms:
        return _expr_transforms[type(arg)](result)
    else:
        return result


def shift_like(name):
    def formatter(translator, op):
        arg, offset, default = op.args

        arg_formatted = translator.translate(arg)

        if default is not None:
            if offset is None:
                offset_formatted = '1'
            else:
                offset_formatted = translator.translate(offset)

            default_formatted = translator.translate(default)

            return '{}({}, {}, {})'.format(
                name, arg_formatted, offset_formatted, default_formatted
            )
        elif offset is not None:
            offset_formatted = translator.translate(offset)
            return f'{name}({arg_formatted}, {offset_formatted})'
        else:
            return f'{name}({arg_formatted})'

    return formatter


def ntile(translator, op):
    return f'ntile({translator.translate(op.buckets)})'
