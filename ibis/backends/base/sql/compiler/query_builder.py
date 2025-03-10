from __future__ import annotations

from io import StringIO

import toolz

import ibis.common.exceptions as com
import ibis.expr.operations as ops
import ibis.expr.types as ir
import ibis.util as util
from ibis.backends.base.sql.compiler.base import DML, QueryAST, SetOp
from ibis.backends.base.sql.compiler.select_builder import (
    SelectBuilder,
    _LimitSpec,
)
from ibis.backends.base.sql.compiler.translator import (
    ExprTranslator,
    QueryContext,
)
from ibis.backends.base.sql.registry import quote_identifier
from ibis.common.grounds import Comparable
from ibis.config import options


class TableSetFormatter:

    _join_names = {
        ops.InnerJoin: 'INNER JOIN',
        ops.LeftJoin: 'LEFT OUTER JOIN',
        ops.RightJoin: 'RIGHT OUTER JOIN',
        ops.OuterJoin: 'FULL OUTER JOIN',
        ops.LeftAntiJoin: 'LEFT ANTI JOIN',
        ops.LeftSemiJoin: 'LEFT SEMI JOIN',
        ops.CrossJoin: 'CROSS JOIN',
    }

    def __init__(self, parent, node, indent=2):
        # `parent` is a `Select` instance, not a `TableSetFormatter`
        self.parent = parent
        self.context = parent.context
        self.node = node
        self.indent = indent

        self.join_tables = []
        self.join_types = []
        self.join_predicates = []

    def _translate(self, expr):
        return self.parent._translate(expr)

    # TODO(kszucs): could use lin.traverse here
    def _walk_join_tree(self, op):
        if util.all_of([op.left, op.right], ops.Join):
            raise NotImplementedError(
                'Do not support joins between ' 'joins yet'
            )

        jname = self._get_join_type(op)

        # Read off tables and join predicates left-to-right in
        # depth-first order
        if isinstance(op.left, ops.Join):
            self._walk_join_tree(op.left)
            self.join_tables.append(self._format_table(op.right))
        elif isinstance(op.right, ops.Join):
            self.join_tables.append(self._format_table(op.left))
            self._walk_join_tree(op.right)
        else:
            # Both tables
            self.join_tables.append(self._format_table(op.left))
            self.join_tables.append(self._format_table(op.right))

        self.join_types.append(jname)
        self.join_predicates.append(op.predicates)

    def _get_join_type(self, op):
        return self._join_names[type(op)]

    def _quote_identifier(self, name):
        return quote_identifier(name)

    def _format_in_memory_table(self, op):
        names = op.schema.names
        raw_rows = (
            ", ".join(
                f"{val!r} AS {self._quote_identifier(name)}"
                for val, name in zip(row, names)
            )
            for row in op.data.to_frame().itertuples(index=False)
        )
        rows = ", ".join(f"({raw_row})" for raw_row in raw_rows)
        return f"(VALUES {rows})"

    def _format_table(self, op):
        # TODO: This could probably go in a class and be significantly nicer
        ctx = self.context

        ref_op = op
        if isinstance(op, ops.SelfReference):
            ref_op = op.table

        if isinstance(ref_op, ops.InMemoryTable):
            result = self._format_in_memory_table(ref_op)
            is_subquery = True
        elif isinstance(ref_op, ops.PhysicalTable):
            name = ref_op.name
            # TODO(kszucs): add a mandatory `name` field to the base
            # PhyisicalTable instead of the child classes, this should prevent
            # this error scenario
            if name is None:
                raise com.RelationError(f'Table did not have a name: {op!r}')
            result = self._quote_identifier(name)
            is_subquery = False
        else:
            # A subquery
            if ctx.is_extracted(ref_op):
                # Was put elsewhere, e.g. WITH block, we just need to grab its
                # alias
                alias = ctx.get_ref(op)

                # HACK: self-references have to be treated more carefully here
                if isinstance(op, ops.SelfReference):
                    return f'{ctx.get_ref(ref_op)} {alias}'
                else:
                    return alias

            subquery = ctx.get_compiled_expr(op)
            result = f'(\n{util.indent(subquery, self.indent)}\n)'
            is_subquery = True

        if is_subquery or ctx.need_aliases(op):
            result += f' {ctx.get_ref(op)}'

        return result

    def get_result(self):
        # Got to unravel the join stack; the nesting order could be
        # arbitrary, so we do a depth first search and push the join tokens
        # and predicates onto a flat list, then format them
        op = self.node

        if isinstance(op, ops.Join):
            self._walk_join_tree(op)
        else:
            self.join_tables.append(self._format_table(op))

        # TODO: Now actually format the things
        buf = StringIO()
        buf.write(self.join_tables[0])
        for jtype, table, preds in zip(
            self.join_types, self.join_tables[1:], self.join_predicates
        ):
            buf.write('\n')
            buf.write(util.indent(f'{jtype} {table}', self.indent))

            fmt_preds = []
            npreds = len(preds)
            for pred in preds:
                new_pred = self._translate(pred)
                if npreds > 1:
                    new_pred = f'({new_pred})'
                fmt_preds.append(new_pred)

            if len(fmt_preds):
                buf.write('\n')

                conj = ' AND\n{}'.format(' ' * 3)
                fmt_preds = util.indent(
                    'ON ' + conj.join(fmt_preds), self.indent * 2
                )
                buf.write(fmt_preds)

        return buf.getvalue()


class Select(DML, Comparable):

    """A SELECT statement which, after execution, might yield back to the user
    a table, array/list, or scalar value, depending on the expression that
    generated it."""

    def __init__(
        self,
        table_set,
        select_set,
        translator_class,
        table_set_formatter_class,
        context,
        subqueries=None,
        where=None,
        group_by=None,
        having=None,
        order_by=None,
        limit=None,
        distinct=False,
        indent=2,
        result_handler=None,
        parent_op=None,
    ):
        self.translator_class = translator_class
        self.table_set_formatter_class = table_set_formatter_class
        self.context = context

        self.select_set = select_set
        self.table_set = table_set
        self.distinct = distinct

        self.parent_op = parent_op

        self.where = where or []

        # Group keys and post-predicates for aggregations
        self.group_by = group_by or []
        self.having = having or []
        self.order_by = order_by or []

        self.limit = limit
        self.subqueries = subqueries or []

        self.indent = indent

        self.result_handler = result_handler

    def _translate(self, expr, named=False, permit_subquery=False):
        translator = self.translator_class(
            expr,
            context=self.context,
            named=named,
            permit_subquery=permit_subquery,
        )
        return translator.get_result()

    def __equals__(self, other: Select) -> bool:
        return (
            self.limit == other.limit
            and self._all_exprs() == other._all_exprs()
        )

    def _all_exprs(self):
        return tuple(
            *self.select_set,
            self.table_set,
            *self.where,
            *self.group_by,
            *self.having,
            *self.order_by,
            *self.subqueries,
        )

    def compile(self):
        """This method isn't yet idempotent; calling multiple times may yield
        unexpected results."""
        # Can't tell if this is a hack or not. Revisit later
        self.context.set_query(self)

        # If any subqueries, translate them and add to beginning of query as
        # part of the WITH section
        with_frag = self.format_subqueries()

        # SELECT
        select_frag = self.format_select_set()

        # FROM, JOIN, UNION
        from_frag = self.format_table_set()

        # WHERE
        where_frag = self.format_where()

        # GROUP BY and HAVING
        groupby_frag = self.format_group_by()

        # ORDER BY
        order_frag = self.format_order_by()

        # LIMIT
        limit_frag = self.format_limit()

        # Glue together the query fragments and return
        query = '\n'.join(
            filter(
                None,
                [
                    with_frag,
                    select_frag,
                    from_frag,
                    where_frag,
                    groupby_frag,
                    order_frag,
                    limit_frag,
                ],
            )
        )
        return query

    def format_subqueries(self):
        if not self.subqueries:
            return

        context = self.context

        buf = []

        for expr in self.subqueries:
            formatted = util.indent(context.get_compiled_expr(expr), 2)
            alias = context.get_ref(expr)
            buf.append(f'{alias} AS (\n{formatted}\n)')

        return 'WITH {}'.format(',\n'.join(buf))

    def format_select_set(self):
        # TODO:
        context = self.context
        formatted = []
        for node in self.select_set:
            if isinstance(node, ops.Value):
                expr_str = self._translate(node, named=True)
            elif isinstance(node, ops.TableNode):
                # A * selection, possibly prefixed
                if context.need_aliases(node):
                    alias = context.get_ref(node)
                    expr_str = f'{alias}.*' if alias else '*'
                else:
                    expr_str = '*'
            else:
                raise TypeError(node)
            formatted.append(expr_str)

        buf = StringIO()
        line_length = 0
        max_length = 70
        tokens = 0
        for i, val in enumerate(formatted):
            # always line-break for multi-line expressions
            if val.count('\n'):
                if i:
                    buf.write(',')
                buf.write('\n')
                indented = util.indent(val, self.indent)
                buf.write(indented)

                # set length of last line
                line_length = len(indented.split('\n')[-1])
                tokens = 1
            elif (
                tokens > 0
                and line_length
                and len(val) + line_length > max_length
            ):
                # There is an expr, and adding this new one will make the line
                # too long
                buf.write(',\n       ') if i else buf.write('\n')
                buf.write(val)
                line_length = len(val) + 7
                tokens = 1
            else:
                if i:
                    buf.write(',')
                buf.write(' ')
                buf.write(val)
                tokens += 1
                line_length += len(val) + 2

        if self.distinct:
            select_key = 'SELECT DISTINCT'
        else:
            select_key = 'SELECT'

        return f'{select_key}{buf.getvalue()}'

    def format_table_set(self):
        if self.table_set is None:
            return None

        fragment = 'FROM '

        helper = self.table_set_formatter_class(self, self.table_set)
        fragment += helper.get_result()

        return fragment

    def format_group_by(self):
        if not len(self.group_by):
            # There is no aggregation, nothing to see here
            return None

        lines = []
        if len(self.group_by) > 0:
            clause = 'GROUP BY {}'.format(
                ', '.join([str(x + 1) for x in self.group_by])
            )
            lines.append(clause)

        if len(self.having) > 0:
            trans_exprs = []
            for expr in self.having:
                translated = self._translate(expr)
                trans_exprs.append(translated)
            lines.append('HAVING {}'.format(' AND '.join(trans_exprs)))

        return '\n'.join(lines)

    def format_where(self):
        if not self.where:
            return None

        buf = StringIO()
        buf.write('WHERE ')
        fmt_preds = []
        npreds = len(self.where)
        for pred in self.where:
            new_pred = self._translate(pred, permit_subquery=True)
            if npreds > 1:
                new_pred = f'({new_pred})'
            fmt_preds.append(new_pred)

        conj = ' AND\n{}'.format(' ' * 6)
        buf.write(conj.join(fmt_preds))
        return buf.getvalue()

    def format_order_by(self):
        if not self.order_by:
            return None

        buf = StringIO()
        buf.write('ORDER BY ')

        formatted = []
        for key in self.order_by:
            translated = self._translate(key.expr)
            suffix = 'ASC' if key.ascending else 'DESC'
            translated += f' {suffix}'
            formatted.append(translated)

        buf.write(', '.join(formatted))
        return buf.getvalue()

    def format_limit(self):
        if not self.limit:
            return None

        buf = StringIO()

        n = self.limit.n
        buf.write(f'LIMIT {n}')
        if offset := self.limit.offset:
            buf.write(f' OFFSET {offset}')

        return buf.getvalue()


class Union(SetOp):
    _keyword = "UNION"


class Intersection(SetOp):
    _keyword = "INTERSECT"


class Difference(SetOp):
    _keyword = "EXCEPT"


def flatten_set_op(op):
    """Extract all union queries from `table`.
    Parameters
    ----------
    table : ops.TableNode
    Returns
    -------
    Iterable[Union[Table, bool]]
    """

    if isinstance(op, ops.SetOp):
        # For some reason mypy considers `op.left` and `op.right`
        # of `Argument` type, and fails the validation. While in
        # `flatten` types are the same, and it works
        return toolz.concatv(
            flatten_set_op(op.left),  # type: ignore
            [op.distinct],
            flatten_set_op(op.right),  # type: ignore
        )
    return [op]


def flatten(op: ops.TableNode):
    """Extract all intersection or difference queries from `table`.
    Parameters
    ----------
    table : Table
    Returns
    -------
    Iterable[Union[Table]]
    """
    return list(
        toolz.concatv(flatten_set_op(op.left), flatten_set_op(op.right))
    )


class Compiler:
    translator_class = ExprTranslator
    context_class = QueryContext
    select_builder_class = SelectBuilder
    table_set_formatter_class = TableSetFormatter
    select_class = Select
    union_class = Union
    intersect_class = Intersection
    difference_class = Difference

    cheap_in_memory_tables = False

    @classmethod
    def make_context(cls, params=None):
        params = params or {}

        unaliased_params = {}
        for expr, value in params.items():
            op = expr.op()
            if isinstance(op, ops.Alias):
                op = op.arg
            unaliased_params[op] = value

        return cls.context_class(compiler=cls, params=unaliased_params)

    @classmethod
    def to_ast(cls, node, context=None):
        # TODO(kszucs): consider to support a single type only
        if isinstance(node, ir.Expr):
            node = node.op()

        if context is None:
            context = cls.make_context()

        # collect setup and teardown queries
        setup_queries = cls._generate_setup_queries(node, context)
        teardown_queries = cls._generate_teardown_queries(node, context)

        # TODO: any setup / teardown DDL statements will need to be done prior
        # to building the result set-generating statements.
        if isinstance(node, ops.Union):
            query = cls._make_union(node, context)
        elif isinstance(node, ops.Intersection):
            query = cls._make_intersect(node, context)
        elif isinstance(node, ops.Difference):
            query = cls._make_difference(node, context)
        else:
            query = cls.select_builder_class().to_select(
                select_class=cls.select_class,
                table_set_formatter_class=cls.table_set_formatter_class,
                node=node,
                context=context,
                translator_class=cls.translator_class,
            )

        return QueryAST(
            context,
            query,
            setup_queries=setup_queries,
            teardown_queries=teardown_queries,
        )

    @classmethod
    def to_ast_ensure_limit(cls, expr, limit=None, params=None):
        context = cls.make_context(params=params)
        query_ast = cls.to_ast(expr, context)

        # note: limit can still be None at this point, if the global
        # default_limit is None
        for query in reversed(query_ast.queries):
            if (
                isinstance(query, Select)
                and not isinstance(expr, ir.Scalar)
                and query.table_set is not None
            ):
                if query.limit is None:
                    if limit == 'default':
                        query_limit = options.sql.default_limit
                    else:
                        query_limit = limit
                    if query_limit:
                        query.limit = _LimitSpec(query_limit, offset=0)
                elif limit is not None and limit != 'default':
                    query.limit = _LimitSpec(limit, query.limit.offset)

        return query_ast

    @classmethod
    def to_sql(cls, node, context=None, params=None):
        # TODO(kszucs): consider to support a single type only
        if isinstance(node, ir.Expr):
            node = node.op()

        assert isinstance(node, ops.Node)

        if context is None:
            context = cls.make_context(params=params)
        return cls.to_ast(node, context).queries[0].compile()

    @staticmethod
    def _generate_setup_queries(expr, context):
        return []

    @staticmethod
    def _generate_teardown_queries(expr, context):
        return []

    @staticmethod
    def _make_set_op(cls, op, context):
        # flatten unions so that we can codegen them all at once
        set_op_info = list(flatten_set_op(op))

        # since op is a union, we have at least 3 elements in union_info (left
        # distinct right) and if there is more than a single union we have an
        # additional two elements per union (distinct right) which means the
        # total number of elements is at least 3 + (2 * number of unions - 1)
        # and is therefore an odd number
        npieces = len(set_op_info)
        assert (
            npieces >= 3 and npieces % 2 != 0
        ), 'Invalid set operation expression'

        # 1. every other object starting from 0 is a Table instance
        # 2. every other object starting from 1 is a bool indicating the type
        #    of $set_op (distinct or not distinct)
        table_exprs, distincts = set_op_info[::2], set_op_info[1::2]
        return cls(table_exprs, op, distincts=distincts, context=context)

    @classmethod
    def _make_union(cls, op, context):
        return cls._make_set_op(cls.union_class, op, context)

    @classmethod
    def _make_intersect(cls, op, context):
        # flatten intersections so that we can codegen them all at once
        return cls._make_set_op(cls.intersect_class, op, context)

    @classmethod
    def _make_difference(cls, op, context):
        # flatten differences so that we can codegen them all at once
        return cls._make_set_op(cls.difference_class, op, context)
