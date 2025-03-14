from __future__ import annotations

import sqlalchemy as sa

import ibis
import ibis.expr.datatypes as dt
import ibis.expr.operations as ops
from ibis.backends.base.sql.alchemy.datatypes import (
    ibis_type_to_sqla,
    to_sqla_type,
)
from ibis.backends.base.sql.alchemy.registry import (
    fixed_arity,
    sqlalchemy_operation_registry,
)
from ibis.backends.base.sql.compiler import ExprTranslator, QueryContext


class AlchemyContext(QueryContext):
    def collapse(self, queries):
        if isinstance(queries, str):
            return queries

        if len(queries) > 1:
            raise NotImplementedError(
                'Only a single query is supported for SQLAlchemy backends'
            )
        return queries[0]

    def subcontext(self):
        return self.__class__(
            compiler=self.compiler,
            parent=self,
            params=self.params,
        )


class AlchemyExprTranslator(ExprTranslator):

    _registry = sqlalchemy_operation_registry
    _rewrites = ExprTranslator._rewrites.copy()
    _type_map = ibis_type_to_sqla

    context_class = AlchemyContext

    _bool_aggs_need_cast_to_int32 = True
    _has_reduction_filter_syntax = False

    integer_to_timestamp = sa.func.to_timestamp
    native_json_type = True
    _always_quote_columns = False

    def name(self, translated, name, force=True):
        return translated.label(name)

    def get_sqla_type(self, data_type):
        return to_sqla_type(data_type, type_map=self._type_map)

    def _maybe_cast_bool(self, op, arg):
        if (
            self._bool_aggs_need_cast_to_int32
            and isinstance(op, (ops.Sum, ops.Mean, ops.Min, ops.Max))
            and isinstance(dtype := arg.output_dtype, dt.Boolean)
        ):
            return ops.Cast(arg, dt.Int32(nullable=dtype.nullable))
        return arg

    def _reduction(self, sa_func, op):
        argtuple = (
            self._maybe_cast_bool(op, arg)
            for name, arg in zip(op.argnames, op.args)
            if isinstance(arg, ops.Node) and name != "where"
        )
        if (where := op.where) is not None:
            if self._has_reduction_filter_syntax:
                sa_args = tuple(map(self.translate, argtuple))
                return sa_func(*sa_args).filter(self.translate(where))
            else:
                # TODO(kszucs): avoid expression roundtrips
                sa_args = tuple(
                    self.translate(where.to_expr().ifelse(arg, None).op())
                    for arg in argtuple
                )
        else:
            sa_args = tuple(map(self.translate, argtuple))

        return sa_func(*sa_args)


rewrites = AlchemyExprTranslator.rewrites


@rewrites(ops.NullIfZero)
def _nullifzero(op):
    # TODO(kszucs): avoid rountripping to expr then back to op
    expr = op.arg.to_expr()
    new_expr = (expr == 0).ifelse(ibis.NA, expr)
    return new_expr.op()


# TODO This was previously implemented with the legacy `@compiles` decorator.
# This definition should now be in the registry, but there is some magic going
# on that things fail if it's not defined here (and in the registry
# `operator.truediv` is used.
def _true_divide(t, op):
    if all(isinstance(arg.output_dtype, dt.Integer) for arg in op.args):
        # TODO(kszucs): this should be done in the rewrite phase
        right, left = op.right.to_expr(), op.left.to_expr()
        new_expr = left.div(right.cast('double'))
        return t.translate(new_expr.op())

    return fixed_arity(lambda x, y: x / y, 2)(t, op)


AlchemyExprTranslator._registry[ops.Divide] = _true_divide
