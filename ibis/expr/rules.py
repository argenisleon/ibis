import builtins
import enum
import operator
from itertools import product, starmap

import ibis.common.exceptions as com
import ibis.expr.datatypes as dt
import ibis.expr.schema as sch
import ibis.expr.types as ir
import ibis.util as util
from ibis.common.annotations import (  # noqa: F401
    initialized,
    optional,
    variadic,
)
from ibis.common.validators import (  # noqa: F401
    bool_,
    instance_of,
    isin,
    map_to,
    one_of,
    ref,
    str_,
    tuple_of,
    validator,
)
from ibis.expr.deferred import Deferred


# TODO(kszucs): consider to rename to datashape
class Shape(enum.IntEnum):
    SCALAR = 0
    COLUMNAR = 1
    # TABULAR = 2


def highest_precedence_shape(nodes):
    if builtins.any(node.output_shape is Shape.COLUMNAR for node in nodes):
        return Shape.COLUMNAR
    else:
        return Shape.SCALAR


def highest_precedence_dtype(nodes):
    """Return the highest precedence type from the passed expressions.

    Also verifies that there are valid implicit casts between any of the types
    and the selected highest precedence type.
    This is a thin wrapper around datatypes highest precedence check.

    Parameters
    ----------
    nodes : Iterable[ops.Value]
      A sequence of Expressions

    Returns
    -------
    dtype: DataType
      The highest precedence datatype
    """
    return dt.highest_precedence(node.output_dtype for node in nodes)


def castable(source, target):
    """Return whether source ir type is implicitly castable to target.

    Based on the underlying datatypes and the value in case of Literals
    """
    value = getattr(source, 'value', None)
    return dt.castable(source.output_dtype, target.output_dtype, value=value)


def comparable(left, right):
    return castable(left, right) or castable(right, left)


class rule(validator):
    __slots__ = ()

    def _erase_expr(self, value):
        return value.op() if isinstance(value, ir.Expr) else value

    def __call__(self, *args, **kwargs):
        args = map(self._erase_expr, args)
        kwargs = {k: self._erase_expr(v) for k, v in kwargs.items()}
        result = super().__call__(*args, **kwargs)
        assert not isinstance(result, ir.Expr)
        return result


# ---------------------------------------------------------------------
# Input type validators / coercion functions


@rule
def just(arg):
    return lambda **_: arg


@rule
def nodes_of(inner, arg, **kwargs):
    import ibis.expr.operations as ops

    values = tuple_of(inner, arg, **kwargs)
    return ops.NodeList(*values)


@rule
def sort_key_from(table_ref, key, **kwargs):
    import ibis.expr.operations as ops

    is_ascending = {
        "asc": True,
        "ascending": True,
        "desc": False,
        "descending": False,
        0: False,
        1: True,
        False: False,
        True: True,
    }

    if callable(key):
        key = function_of(table_ref, key, **kwargs)

    if isinstance(key, ops.SortKey):
        return key
    elif isinstance(key, tuple):
        key, order = key
    else:
        key, order = key, True

    key = one_of(
        (function_of(table_ref), column_from(table_ref), any),
        key,
        **kwargs,
    )

    if isinstance(order, str):
        order = order.lower()
    order = map_to(is_ascending, order)

    return ops.SortKey(key, ascending=order)


@rule
def datatype(arg, **kwargs):
    return dt.dtype(arg)


# TODO(kszucs): make type argument the first and mandatory, similarly to the
# value rule, move out the type inference to `ir.literal()` method
# TODO(kszucs): may not make sense to support an explicit datatype here, we
# could do the coercion in the API function ibis.literal()
@rule
def literal(dtype, value, **kwargs):
    import ibis.expr.operations as ops

    if isinstance(value, ops.Literal):
        return value

    try:
        inferred_dtype = dt.infer(value)
    except com.InputTypeError:
        has_inferred = False
    else:
        has_inferred = True

    if dtype is None:
        has_explicit = False
    else:
        has_explicit = True
        # TODO(kszucs): handle class-like dtype definitions here explicitly
        explicit_dtype = dt.dtype(dtype)

    if has_explicit and has_inferred:
        try:
            # ensure type correctness: check that the inferred dtype is
            # implicitly castable to the explicitly given dtype and value
            dtype = inferred_dtype.cast(explicit_dtype, value=value)
        except com.IbisTypeError:
            raise TypeError(
                f'Value {value!r} cannot be safely coerced to {type}'
            )
    elif has_explicit:
        dtype = explicit_dtype
    elif has_inferred:
        dtype = inferred_dtype
    else:
        raise com.IbisTypeError(
            'The datatype of value {!r} cannot be inferred, try '
            'passing it explicitly with the `type` keyword.'.format(value)
        )

    if isinstance(dtype, dt.Null):
        return ops.NullLiteral()

    value = dt.normalize(dtype, value)
    return ops.Literal(value, dtype=dtype)


@rule
def value(dtype, arg, **kwargs):
    """Validates that the given argument is a Value with a particular datatype.

    Parameters
    ----------
    dtype : DataType subclass or DataType instance
    arg : python literal or an ibis expression
      If a python literal is given the validator tries to coerce it to an ibis
      literal.

    Returns
    -------
    arg : Value
      An ibis value expression with the specified datatype
    """
    import ibis.expr.operations as ops

    if not isinstance(arg, ops.Value):
        # coerce python literal to ibis literal
        arg = literal(None, arg)

    if dtype is None:
        # no datatype restriction
        return arg
    elif isinstance(dtype, type):
        # dtype class has been specified like dt.Interval or dt.Decimal
        if not issubclass(dtype, dt.DataType):
            raise com.IbisTypeError(
                f"Datatype specification {dtype} is not a subclass dt.DataType"
            )
        elif isinstance(arg.output_dtype, dtype):
            return arg
        else:
            raise com.IbisTypeError(
                f'Given argument with datatype {arg.output_dtype} is not '
                f'subtype of {dtype}'
            )
    elif isinstance(dtype, (dt.DataType, str)):
        # dtype instance or string has been specified and arg's dtype is
        # implicitly castable to it, like dt.int8 is castable to dt.int64
        dtype = dt.dtype(dtype)
        # retrieve literal values for implicit cast check
        value = getattr(arg, 'value', None)
        if dt.castable(arg.output_dtype, dtype, value=value):
            return arg
        else:
            raise com.IbisTypeError(
                f'Given argument with datatype {arg.output_dtype} is not '
                f'implicitly castable to {dtype}'
            )
    else:
        raise com.IbisTypeError(f'Invalid datatype specification {dtype}')


@rule
def scalar(inner, arg, **kwargs):
    arg = inner(arg, **kwargs)
    if arg.output_shape is Shape.SCALAR:
        return arg
    else:
        raise com.IbisTypeError(f"{arg} it not a scalar")


@rule
def column(inner, arg, **kwargs):
    arg = inner(arg, **kwargs)
    if arg.output_shape is Shape.COLUMNAR:
        return arg
    else:
        raise com.IbisTypeError(f"{arg} it not a column")


any = value(None)
double = value(dt.double)
string = value(dt.string)
boolean = value(dt.boolean)
integer = value(dt.int64)
decimal = value(dt.Decimal)
floating = value(dt.float64)
date = value(dt.date)
time = value(dt.time)
timestamp = value(dt.Timestamp)
category = value(dt.category)
temporal = one_of([timestamp, date, time])
json = value(dt.json)

strict_numeric = one_of([integer, floating, decimal])
soft_numeric = one_of([integer, floating, decimal, boolean])
numeric = soft_numeric

set_ = value(dt.Set)
array = value(dt.Array)
struct = value(dt.Struct)
mapping = value(dt.Map)

geospatial = value(dt.GeoSpatial)
point = value(dt.Point)
linestring = value(dt.LineString)
polygon = value(dt.Polygon)
multilinestring = value(dt.MultiLineString)
multipoint = value(dt.MultiPoint)
multipolygon = value(dt.MultiPolygon)


@rule
def interval(arg, units=None, **kwargs):
    arg = value(dt.Interval, arg)
    unit = arg.output_dtype.unit
    if units is not None and unit not in units:
        msg = 'Interval unit `{}` is not among the allowed ones {}'
        raise com.IbisTypeError(msg.format(unit, units))
    return arg


@validator
def client(arg, **kwargs):
    from ibis.backends.base import BaseBackend

    return instance_of(BaseBackend, arg)


# ---------------------------------------------------------------------
# Ouput type functions


def dtype_like(name):
    @initialized
    def output_dtype(self):
        args = getattr(self, name)
        args = args if util.is_iterable(args) else [args]
        return highest_precedence_dtype(args)

    return output_dtype


def shape_like(name):
    @initialized
    def output_shape(self):
        args = getattr(self, name)
        args = args if util.is_iterable(args) else [args]
        return highest_precedence_shape(args)

    return output_shape


# TODO(kszucs): might just use bounds instead of actual literal values
# that could simplify interval binop output_type methods
# TODO(kszucs): pre-generate mapping?


def _promote_integral_binop(exprs, op):
    bounds, dtypes = [], []
    for arg in exprs:
        dtypes.append(arg.output_dtype)
        if hasattr(arg, 'value'):
            # arg.op() is a literal
            bounds.append([arg.value])
        else:
            bounds.append(arg.output_dtype.bounds)

    all_unsigned = dtypes and util.all_of(dtypes, dt.UnsignedInteger)
    # In some cases, the bounding type might be int8, even though neither
    # of the types are that small. We want to ensure the containing type is
    # _at least_ as large as the smallest type in the expression.
    values = list(starmap(op, product(*bounds)))
    dtypes.extend(dt.infer(v, prefer_unsigned=all_unsigned) for v in values)
    return dt.highest_precedence(dtypes)


def _promote_decimal_binop(args, op):
    if len(args) != 2:
        return highest_precedence_dtype(args)

    # TODO: Add support for setting the maximum precision and maximum scale
    left = args[0].output_dtype
    right = args[1].output_dtype

    max_prec = 31 if left.precision <= 31 and right.precision <= 31 else 63
    max_scale = 31

    if op is operator.mul:
        return dt.Decimal(
            min(max_prec, left.precision + right.precision),
            min(max_scale, left.scale + right.scale),
        )
    elif op is operator.add or op is operator.sub:
        return dt.Decimal(
            min(
                max_prec,
                max(
                    left.precision - left.scale,
                    right.precision - right.scale,
                )
                + max(left.scale, right.scale)
                + 1,
            ),
            max(left.scale, right.scale),
        )
    else:
        return highest_precedence_dtype(args)


def numeric_like(name, op):
    @initialized
    def output_dtype(self):
        args = getattr(self, name)
        dtypes = [arg.output_dtype for arg in args]
        if util.all_of(dtypes, dt.Integer):
            result = _promote_integral_binop(args, op)
        elif util.all_of(dtypes, dt.Decimal):
            result = _promote_decimal_binop(args, op)
        else:
            result = highest_precedence_dtype(args)

        return result

    return output_dtype


# TODO(kszucs): it could be as simple as rlz.instance_of(ops.TableNode)
# we have a single test case testing the schema superset condition, not
# used anywhere else
@rule
def table(arg, schema=None, **kwargs):
    """A table argument.

    Parameters
    ----------
    schema
        A validator for the table's columns. Only column subset validators are
        currently supported. Accepts any arguments that `sch.schema` accepts.
        See the example for usage.
    arg
        An argument

    The following op will accept an argument named `'table'`. Note that the
    `schema` argument specifies rules for columns that are required to be in
    the table: `time`, `group` and `value1`. These must match the types
    specified in the column rules. Column `value2` is optional, but if present
    it must be of the specified type. The table may have extra columns not
    specified in the schema.
    """
    import ibis.expr.operations as ops

    if not isinstance(arg, ops.TableNode):
        raise com.IbisTypeError(
            f'Argument is not a table; got type {type(arg).__name__}'
        )

    if schema is not None:
        if arg.schema >= sch.schema(schema):
            return arg

        raise com.IbisTypeError(
            f'Argument is not a table with column subset of {schema}'
        )
    return arg


@rule
def column_from(table_ref, column, **kwargs):
    """A column from a named table.

    This validator accepts columns passed as string, integer, or column
    expression. In the case of a column expression, this validator
    checks if the column in the table is equal to the column being
    passed.
    """
    import ibis.expr.operations as ops

    # TODO(kszucs): should avoid converting to TableExpr
    table = table_ref(**kwargs).to_expr()

    # TODO(kszucs): should avoid converting to a ColumnExpr
    if isinstance(column, ops.Node):
        column = column.to_expr()

    column = table._ensure_expr(column)

    if not isinstance(column, ir.Column):
        raise com.IbisTypeError(
            "value must be an int or str or Column, got "
            f"{type(column).__name__}"
        )

    if not column.has_name():
        raise com.IbisTypeError(f"Passed column {column} has no name")

    maybe_column = column.get_name()
    try:
        if column.equals(table[maybe_column]):
            return column.op()
        else:
            raise com.IbisTypeError(
                f"Passed column is not a column in {type(table)}"
            )
    except com.IbisError:
        raise com.IbisTypeError(
            f"Cannot get column {maybe_column} from {type(table)}"
        )


@rule
def base_table_of(table_ref, *, this, strict=True):
    from ibis.expr.analysis import find_first_base_table

    arg = table_ref(this=this)
    base = find_first_base_table(arg)
    if strict and base is None:
        raise com.IbisTypeError(f"`{arg}` doesn't have a base table")
    return base


@rule
def function_of(table_ref, fn, *, output_rule=any, this=None):
    arg = table_ref(this=this).to_expr()

    if util.is_function(fn):
        arg = fn(arg)
    elif isinstance(fn, Deferred):
        arg = fn.resolve(arg)
    else:
        raise com.IbisTypeError(
            'argument `fn` must be a function, lambda or deferred operation'
        )

    return output_rule(arg, this=this)


@rule
def reduction(arg, **kwargs):
    from ibis.expr.analysis import is_reduction

    if not is_reduction(arg):
        raise com.IbisTypeError("`argument` must be a reduction")

    return arg


@rule
def non_negative_integer(arg, **kwargs):
    if not isinstance(arg, int):
        raise com.IbisTypeError(
            f"positive integer must be int type, got {type(arg).__name__}"
        )
    if arg < 0:
        raise ValueError("got negative value for non-negative integer rule")
    return arg


@rule
def pair(inner_left, inner_right, arg, **kwargs):
    try:
        a, b = arg
    except TypeError:
        raise com.IbisTypeError(f"{arg} is not an iterable with two elements")
    return inner_left(a[0], **kwargs), inner_right(b, **kwargs)


@rule
def analytic(arg, **kwargs):
    from ibis.expr.analysis import is_analytic

    if not is_analytic(arg):
        raise com.IbisInputError(
            'Expression does not contain a valid window operation'
        )
    return arg


@validator
def window_from(table_ref, win, **kwargs):
    from ibis.expr.window import Window

    if not isinstance(win, Window):
        raise com.IbisTypeError(
            "`win` argument should be of type `ibis.expr.window.Window`; "
            f"got type {type(win).__name__}"
        )

    table = table_ref(**kwargs)
    if table is not None:
        win = win.bind(table.to_expr())

    if win.max_lookback is not None:
        error_msg = (
            "'max lookback' windows must be ordered " "by a timestamp column"
        )
        if len(win._order_by) != 1:
            raise com.IbisInputError(error_msg)
        order_var = win._order_by[0].args[0]
        if not isinstance(order_var.output_dtype, dt.Timestamp):
            raise com.IbisInputError(error_msg)
    return win
