from __future__ import annotations

from public import public

import ibis.expr.datatypes as dt
import ibis.expr.rules as rlz
from ibis.common.annotations import initialized
from ibis.expr.operations.core import Node, Value
from ibis.expr.window import propagate_down_window


@public
class Window(Value):
    expr = rlz.analytic
    window = rlz.window_from(rlz.base_table_of(rlz.ref("expr"), strict=False))

    output_dtype = rlz.dtype_like("expr")
    output_shape = rlz.Shape.COLUMNAR

    def __init__(self, expr, window):
        expr = propagate_down_window(expr, window)
        super().__init__(expr=expr, window=window)

    def over(self, window):
        new_window = self.window.combine(window)
        return Window(self.expr, new_window)

    @property
    def name(self):
        return self.expr.name


@public
class Analytic(Value):
    output_shape = rlz.Shape.COLUMNAR


@public
class ShiftBase(Analytic):
    arg = rlz.column(rlz.any)

    offset = rlz.optional(rlz.one_of((rlz.integer, rlz.interval)))
    default = rlz.optional(rlz.any)

    output_dtype = rlz.dtype_like("arg")


@public
class Lag(ShiftBase):
    pass


@public
class Lead(ShiftBase):
    pass


@public
class RankBase(Analytic):
    output_dtype = dt.int64


@public
class MinRank(RankBase):
    """Compute position of first element within each equal-value group in
    sorted order. Equivalent to SQL RANK().

    Examples
    --------
    values   ranks
    1        0
    1        0
    2        2
    2        2
    2        2
    3        5

    Returns
    -------
    Int64Column
        The min rank
    """

    arg = rlz.column(rlz.any)


@public
class DenseRank(RankBase):
    """Compute position of first element within each equal-value group in
    sorted order, ignoring duplicate values. Equivalent to SQL DENSE_RANK().

    Examples
    --------
    values   ranks
    1        0
    1        0
    2        1
    2        1
    2        1
    3        2

    Returns
    -------
    IntegerColumn
        The rank
    """

    arg = rlz.column(rlz.any)


@public
class RowNumber(RankBase):
    """Compute row number starting from 0 after sorting by column expression.
    Equivalent to SQL ROW_NUMBER().

    Examples
    --------
    >>> import ibis
    >>> t = ibis.table([('values', dt.int64)])
    >>> w = ibis.window(order_by=t.values)
    >>> row_num = ibis.row_number().over(w)
    >>> result = t[t.values, row_num.name('row_num')]

    Returns
    -------
    IntegerColumn
        Row number
    """


@public
class CumulativeOp(Analytic):
    pass


@public
class CumulativeSum(CumulativeOp):
    """Cumulative sum.

    Requires an ordering window.
    """

    arg = rlz.column(rlz.numeric)

    @initialized
    def output_dtype(self):
        return dt.higher_precedence(self.arg.output_dtype.largest, dt.int64)


@public
class CumulativeMean(CumulativeOp):
    """Cumulative mean.

    Requires an order window.
    """

    arg = rlz.column(rlz.numeric)

    @initialized
    def output_dtype(self):
        return dt.higher_precedence(self.arg.output_dtype.largest, dt.float64)


@public
class CumulativeMax(CumulativeOp):
    """Cumulative max.

    Requires an order window.
    """

    arg = rlz.column(rlz.any)
    output_dtype = rlz.dtype_like("arg")


@public
class CumulativeMin(CumulativeOp):
    """Cumulative min.

    Requires an order window.
    """

    arg = rlz.column(rlz.any)
    output_dtype = rlz.dtype_like("arg")


@public
class CumulativeAny(CumulativeOp):
    arg = rlz.column(rlz.boolean)
    output_dtype = rlz.dtype_like("arg")


@public
class CumulativeAll(CumulativeOp):
    arg = rlz.column(rlz.boolean)
    output_dtype = rlz.dtype_like("arg")


@public
class PercentRank(Analytic):
    arg = rlz.column(rlz.any)
    output_dtype = dt.double


@public
class CumeDist(Analytic):
    arg = rlz.column(rlz.any)
    output_dtype = dt.double


@public
class NTile(Analytic):
    arg = rlz.column(rlz.any)
    buckets = rlz.scalar(rlz.integer)
    output_dtype = dt.int64


@public
class FirstValue(Analytic):
    """Retrieve the first element."""

    arg = rlz.column(rlz.any)
    output_dtype = rlz.dtype_like("arg")


@public
class LastValue(Analytic):
    """Retrieve the last element."""

    arg = rlz.column(rlz.any)
    output_dtype = rlz.dtype_like("arg")


@public
class NthValue(Analytic):
    """Retrieve the Nth element."""

    arg = rlz.column(rlz.any)
    nth = rlz.integer
    output_dtype = rlz.dtype_like("arg")


# TODO(kszucs): should inherit from analytic base
@public
class TopK(Node):
    arg = rlz.column(rlz.any)
    k = rlz.non_negative_integer
    by = rlz.one_of(
        (rlz.function_of(rlz.base_table_of(rlz.ref("arg"))), rlz.any)
    )

    def to_expr(self):
        import ibis.expr.types as ir

        return ir.TopK(self)


public(WindowOp=Window, AnalyticOp=Analytic)
