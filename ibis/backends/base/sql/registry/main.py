import ibis.common.exceptions as com
import ibis.expr.datatypes as dt
import ibis.expr.operations as ops
import ibis.expr.types as ir
import ibis.util as util
from ibis.backends.base.sql.registry import (
    aggregate,
    binary_infix,
    case,
    helpers,
    string,
    timestamp,
    window,
)
from ibis.backends.base.sql.registry.literal import literal, null_literal


def alias(translator, op):
    # just compile the underlying argument because the naming is handled
    # by the translator for the top level expression
    return translator.translate(op.arg)


def fixed_arity(func_name, arity):
    def formatter(translator, op):
        if arity != len(op.args):
            raise com.IbisError('incorrect number of args')
        return helpers.format_call(translator, func_name, *op.args)

    return formatter


def unary(func_name):
    return fixed_arity(func_name, 1)


def not_null(translator, op):
    formatted_arg = translator.translate(op.arg)
    return f'{formatted_arg} IS NOT NULL'


def is_null(translator, op):
    formatted_arg = translator.translate(op.arg)
    return f'{formatted_arg} IS NULL'


def not_(translator, op):
    (arg,) = op.args
    formatted_arg = translator.translate(arg)
    if helpers.needs_parens(arg):
        formatted_arg = helpers.parenthesize(formatted_arg)
    return f'NOT {formatted_arg}'


def negate(translator, op):
    arg = op.args[0]
    formatted_arg = translator.translate(arg)
    if isinstance(op.output_dtype, dt.Boolean):
        return not_(translator, op)
    else:
        if helpers.needs_parens(arg):
            formatted_arg = helpers.parenthesize(formatted_arg)
        return f'-{formatted_arg}'


def ifnull_workaround(translator, op):
    a, b = op.args

    # work around per #345, #360
    if isinstance(a, ir.DecimalValue) and isinstance(b, ir.IntegerValue):
        b = b.cast(a.type())

    return helpers.format_call(translator, 'isnull', a, b)


def sign(translator, op):
    translated_arg = translator.translate(op.arg)
    dtype = op.output_dtype
    translated_type = helpers.type_to_sql_string(dtype)
    if not isinstance(dtype, dt.Float32):
        return f'CAST(sign({translated_arg}) AS {translated_type})'
    return f'sign({translated_arg})'


def hashbytes(translator, op):
    how = op.how

    arg_formatted = translator.translate(op.arg)

    if how == 'md5':
        return f'md5({arg_formatted})'
    elif how == 'sha1':
        return f'sha1({arg_formatted})'
    elif how == 'sha256':
        return f'sha256({arg_formatted})'
    elif how == 'sha512':
        return f'sha512({arg_formatted})'
    else:
        raise NotImplementedError(how)


def log(translator, op):
    arg_formatted = translator.translate(op.arg)

    if op.base is None:
        return f'ln({arg_formatted})'

    base_formatted = translator.translate(op.base)
    return f'log({base_formatted}, {arg_formatted})'


def value_list(translator, op):
    formatted = [translator.translate(x) for x in op.values]
    return helpers.parenthesize(', '.join(formatted))


def cast(translator, op):
    arg_formatted = translator.translate(op.arg)

    if isinstance(op.arg.output_dtype, dt.Category) and op.to == dt.int32:
        return arg_formatted
    if (
        isinstance(op.arg.output_dtype, (dt.Timestamp, dt.Date, dt.Time))
        and op.to == dt.int64
    ):
        return f'1000000 * unix_timestamp({arg_formatted})'
    else:
        sql_type = helpers.type_to_sql_string(op.to)
        return f'CAST({arg_formatted} AS {sql_type})'


def varargs(func_name):
    def varargs_formatter(translator, op):
        return helpers.format_call(translator, func_name, *op.arg.values)

    return varargs_formatter


def between(translator, op):
    comp = translator.translate(op.arg)
    lower = translator.translate(op.lower_bound)
    upper = translator.translate(op.upper_bound)
    return f'{comp} BETWEEN {lower} AND {upper}'


def table_array_view(translator, op):
    ctx = translator.context
    query = ctx.get_compiled_expr(op.table)
    return f'(\n{util.indent(query, ctx.indent)}\n)'


def table_column(translator, op):
    quoted_name = helpers.quote_identifier(op.name, force=True)

    ctx = translator.context

    # If the column does not originate from the table set in the current SELECT
    # context, we should format as a subquery
    if translator.permit_subquery and ctx.is_foreign_expr(op.table):
        # TODO(kszucs): avoid the expression roundtrip
        proj_expr = op.table.to_expr().projection([op.name]).to_array().op()
        return table_array_view(translator, proj_expr)

    if ctx.need_aliases():
        alias = ctx.get_ref(op.table)
        if alias is not None:
            quoted_name = f'{alias}.{quoted_name}'

    return quoted_name


def exists_subquery(translator, op):
    ctx = translator.context

    dummy = ir.literal(1).name(ir.core.unnamed)

    filtered = op.foreign_table.to_expr().filter(
        [pred.to_expr() for pred in op.predicates]
    )
    node = filtered.projection([dummy]).op()

    subquery = ctx.get_compiled_expr(node)

    if isinstance(op, ops.ExistsSubquery):
        key = 'EXISTS'
    elif isinstance(op, ops.NotExistsSubquery):
        key = 'NOT EXISTS'
    else:
        raise NotImplementedError

    return f'{key} (\n{util.indent(subquery, ctx.indent)}\n)'


# XXX this is not added to operation_registry, but looks like impala is
# using it in the tests, and it works, even if it's not imported anywhere
def round(translator, op):
    arg, digits = op.args

    arg_formatted = translator.translate(arg)

    if digits is not None:
        digits_formatted = translator.translate(digits)
        return f'round({arg_formatted}, {digits_formatted})'
    return f'round({arg_formatted})'


# XXX this is not added to operation_registry, but looks like impala is
# using it in the tests, and it works, even if it's not imported anywhere
def hash(translator, op):
    how = op.how

    arg_formatted = translator.translate(op.arg)

    if how == 'fnv':
        return f'fnv_hash({arg_formatted})'
    else:
        raise NotImplementedError(how)


def concat(translator, op):
    joined_args = ', '.join(map(translator.translate, op.arg.values))
    return f"concat({joined_args})"


def sort_key(translator, op):
    suffix = "ASC" if op.ascending else "DESC"
    return f"{translator.translate(op.expr)} {suffix}"


def count_star(translator, op):
    return aggregate._reduction_format(
        translator,
        "count",
        op.where,
        ops.Literal(value=1, dtype=dt.int64),
    )


binary_infix_ops = {
    # Binary operations
    ops.Add: binary_infix.binary_infix_op('+'),
    ops.Subtract: binary_infix.binary_infix_op('-'),
    ops.Multiply: binary_infix.binary_infix_op('*'),
    ops.Divide: binary_infix.binary_infix_op('/'),
    ops.Power: fixed_arity('pow', 2),
    ops.Modulus: binary_infix.binary_infix_op('%'),
    # Comparisons
    ops.Equals: binary_infix.binary_infix_op('='),
    ops.NotEquals: binary_infix.binary_infix_op('!='),
    ops.GreaterEqual: binary_infix.binary_infix_op('>='),
    ops.Greater: binary_infix.binary_infix_op('>'),
    ops.LessEqual: binary_infix.binary_infix_op('<='),
    ops.Less: binary_infix.binary_infix_op('<'),
    ops.IdenticalTo: binary_infix.identical_to,
    # Boolean comparisons
    ops.And: binary_infix.binary_infix_op('AND'),
    ops.Or: binary_infix.binary_infix_op('OR'),
    ops.Xor: binary_infix.xor,
    # bitwise operations
    ops.BitwiseAnd: fixed_arity('bitand', 2),
    ops.BitwiseOr: fixed_arity('bitor', 2),
    ops.BitwiseXor: fixed_arity('bitxor', 2),
    ops.BitwiseLeftShift: fixed_arity('shiftleft', 2),
    ops.BitwiseRightShift: fixed_arity('shiftright', 2),
    ops.BitwiseNot: unary('bitnot'),
}


operation_registry = {
    ops.Alias: alias,
    # Unary operations
    ops.NotNull: not_null,
    ops.IsNull: is_null,
    ops.Negate: negate,
    ops.Not: not_,
    ops.IsNan: unary('is_nan'),
    ops.IsInf: unary('is_inf'),
    ops.IfNull: ifnull_workaround,
    ops.NullIf: fixed_arity('nullif', 2),
    ops.ZeroIfNull: unary('zeroifnull'),
    ops.NullIfZero: unary('nullifzero'),
    ops.Abs: unary('abs'),
    ops.BaseConvert: fixed_arity('conv', 3),
    ops.Ceil: unary('ceil'),
    ops.Floor: unary('floor'),
    ops.Exp: unary('exp'),
    ops.Round: round,
    ops.Sign: sign,
    ops.Sqrt: unary('sqrt'),
    ops.Hash: hash,
    ops.HashBytes: hashbytes,
    ops.Log: log,
    ops.Ln: unary('ln'),
    ops.Log2: unary('log2'),
    ops.Log10: unary('log10'),
    ops.Acos: unary("acos"),
    ops.Asin: unary("asin"),
    ops.Atan: unary("atan"),
    ops.Atan2: fixed_arity("atan2", 2),
    ops.Cos: unary("cos"),
    ops.Cot: unary("cot"),
    ops.Sin: unary("sin"),
    ops.Tan: unary("tan"),
    ops.Pi: fixed_arity("pi", 0),
    ops.E: fixed_arity("exp(1)", 0),
    ops.DecimalPrecision: unary('precision'),
    ops.DecimalScale: unary('scale'),
    # Unary aggregates
    ops.CMSMedian: aggregate.reduction('appx_median'),
    ops.HLLCardinality: aggregate.reduction('ndv'),
    ops.ApproxMedian: aggregate.reduction('appx_median'),
    ops.ApproxCountDistinct: aggregate.reduction('ndv'),
    ops.Mean: aggregate.reduction('avg'),
    ops.Sum: aggregate.reduction('sum'),
    ops.Max: aggregate.reduction('max'),
    ops.Min: aggregate.reduction('min'),
    ops.StandardDev: aggregate.variance_like('stddev'),
    ops.Variance: aggregate.variance_like('var'),
    ops.GroupConcat: aggregate.reduction('group_concat'),
    ops.Count: aggregate.reduction('count'),
    ops.CountStar: count_star,
    ops.CountDistinct: aggregate.count_distinct,
    # string operations
    ops.StringConcat: concat,
    ops.StringLength: unary('length'),
    ops.StringAscii: unary('ascii'),
    ops.Lowercase: unary('lower'),
    ops.Uppercase: unary('upper'),
    ops.Reverse: unary('reverse'),
    ops.Strip: unary('trim'),
    ops.LStrip: unary('ltrim'),
    ops.RStrip: unary('rtrim'),
    ops.Capitalize: unary('initcap'),
    ops.Substring: string.substring,
    ops.StrRight: fixed_arity('strright', 2),
    ops.Repeat: fixed_arity('repeat', 2),
    ops.StringFind: string.string_find,
    ops.Translate: fixed_arity('translate', 3),
    ops.FindInSet: string.find_in_set,
    ops.LPad: fixed_arity('lpad', 3),
    ops.RPad: fixed_arity('rpad', 3),
    ops.StringJoin: string.string_join,
    ops.StringSQLLike: string.string_like,
    ops.RegexSearch: fixed_arity('regexp_like', 2),
    ops.RegexExtract: fixed_arity('regexp_extract', 3),
    ops.RegexReplace: fixed_arity('regexp_replace', 3),
    ops.ParseURL: string.parse_url,
    ops.StartsWith: string.startswith,
    ops.EndsWith: string.endswith,
    ops.StringReplace: fixed_arity('replace', 3),
    # Timestamp operations
    ops.Date: unary('to_date'),
    ops.TimestampNow: lambda *args: 'now()',
    ops.ExtractYear: timestamp.extract_field('year'),
    ops.ExtractMonth: timestamp.extract_field('month'),
    ops.ExtractDay: timestamp.extract_field('day'),
    ops.ExtractQuarter: timestamp.extract_field('quarter'),
    ops.ExtractEpochSeconds: timestamp.extract_epoch_seconds,
    ops.ExtractWeekOfYear: fixed_arity('weekofyear', 1),
    ops.ExtractHour: timestamp.extract_field('hour'),
    ops.ExtractMinute: timestamp.extract_field('minute'),
    ops.ExtractSecond: timestamp.extract_field('second'),
    ops.ExtractMillisecond: timestamp.extract_field('millisecond'),
    ops.TimestampTruncate: timestamp.truncate,
    ops.DateTruncate: timestamp.truncate,
    ops.IntervalFromInteger: timestamp.interval_from_integer,
    # Other operations
    ops.E: lambda *args: 'e()',
    ops.Literal: literal,
    ops.NullLiteral: null_literal,
    ops.NodeList: value_list,
    ops.Cast: cast,
    ops.Coalesce: varargs('coalesce'),
    ops.Greatest: varargs('greatest'),
    ops.Least: varargs('least'),
    ops.Where: fixed_arity('if', 3),
    ops.Between: between,
    ops.Contains: binary_infix.contains("IN"),
    ops.NotContains: binary_infix.contains("NOT IN"),
    ops.SimpleCase: case.simple_case,
    ops.SearchedCase: case.searched_case,
    ops.TableColumn: table_column,
    ops.TableArrayView: table_array_view,
    ops.DateAdd: timestamp.timestamp_op('date_add'),
    ops.DateSub: timestamp.timestamp_op('date_sub'),
    ops.DateDiff: timestamp.timestamp_op('datediff'),
    ops.TimestampAdd: timestamp.timestamp_op('date_add'),
    ops.TimestampSub: timestamp.timestamp_op('date_sub'),
    ops.TimestampDiff: timestamp.timestamp_diff,
    ops.TimestampFromUNIX: timestamp.timestamp_from_unix,
    ops.ExistsSubquery: exists_subquery,
    ops.NotExistsSubquery: exists_subquery,
    # RowNumber, and rank functions starts with 0 in Ibis-land
    ops.RowNumber: lambda *_: 'row_number()',
    ops.DenseRank: lambda *_: 'dense_rank()',
    ops.MinRank: lambda *_: 'rank()',
    ops.PercentRank: lambda *_: 'percent_rank()',
    ops.CumeDist: lambda *_: 'cume_dist()',
    ops.FirstValue: unary('first_value'),
    ops.LastValue: unary('last_value'),
    ops.Lag: window.shift_like('lag'),
    ops.Lead: window.shift_like('lead'),
    ops.Window: window.window,
    ops.NTile: window.ntile,
    ops.DayOfWeekIndex: timestamp.day_of_week_index,
    ops.DayOfWeekName: timestamp.day_of_week_name,
    ops.Strftime: timestamp.strftime,
    ops.SortKey: sort_key,
    **binary_infix_ops,
}
