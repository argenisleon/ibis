from __future__ import annotations

import functools
from typing import NamedTuple

import toolz

import ibis.common.exceptions as com
import ibis.expr.analysis as L
import ibis.expr.datatypes as dt
import ibis.expr.operations as ops
import ibis.util as util
from ibis.backends.base.sql.compiler.base import (
    _extract_common_table_expressions,
)
from ibis.expr.rules import Shape


class _LimitSpec(NamedTuple):
    n: int
    offset: int


class _CorrelatedRefCheck:
    def __init__(self, query, node):
        self.query = query
        self.ctx = query.context
        self.node = node
        self.query_roots = frozenset(
            L.find_immediate_parent_tables(self.query.table_set)
        )
        self.has_foreign_root = False
        self.has_query_root = False

    def get_result(self):
        self.visit(self.node, in_subquery=False)
        return self.has_query_root and self.has_foreign_root

    def visit(self, node, in_subquery):
        in_subquery |= self.is_subquery(node)

        args = node if isinstance(node, ops.NodeList) else node.args

        for arg in args:
            if isinstance(arg, ops.TableNode):
                self.visit_table(arg, in_subquery=in_subquery)
            elif isinstance(arg, ops.Node):
                self.visit(arg, in_subquery=in_subquery)

    def is_subquery(self, node):
        return isinstance(
            node,
            (
                ops.TableArrayView,
                ops.ExistsSubquery,
                ops.NotExistsSubquery,
            ),
        ) or (
            isinstance(node, ops.TableColumn) and not self.is_root(node.table)
        )

    def visit_table(self, node, in_subquery):
        if isinstance(node, (ops.PhysicalTable, ops.SelfReference)):
            self.ref_check(node, in_subquery=in_subquery)

        for arg in node.args:
            # TODO(kszucs): shouldn't be required since ops.NodeList is
            # properly traversable, but otherwise there will be more table
            # references than expected (probably has something to do with the
            # traversal order)
            if isinstance(arg, ops.NodeList):
                for item in arg:
                    self.visit(item, in_subquery=in_subquery)
            elif isinstance(arg, ops.Node):
                self.visit(arg, in_subquery=in_subquery)

    def ref_check(self, node, in_subquery) -> None:
        ctx = self.ctx

        is_root = self.is_root(node)

        self.has_query_root |= is_root and in_subquery
        self.has_foreign_root |= not is_root and in_subquery

        if (
            not is_root
            and not ctx.has_ref(node)
            and (not in_subquery or ctx.has_ref(node, parent_contexts=True))
        ):
            ctx.make_alias(node)

    def is_root(self, what: ops.TableNode) -> bool:
        return what in self.query_roots


def _get_scalar(field):
    def scalar_handler(results):
        return results[field][0]

    return scalar_handler


def _get_column(name):
    def column_handler(results):
        return results[name]

    return column_handler


class SelectBuilder:

    """Transforms expression IR to a query pipeline (potentially multiple
    queries). There will typically be a primary SELECT query, perhaps with some
    subqueries and other DDL to ingest and tear down intermediate data sources.

    Walks the expression tree and catalogues distinct query units,
    builds select statements (and other DDL types, where necessary), and
    records relevant query unit aliases to be used when actually
    generating SQL.
    """

    def to_select(
        self,
        select_class,
        table_set_formatter_class,
        node,
        context,
        translator_class,
    ):
        self.select_class = select_class
        self.table_set_formatter_class = table_set_formatter_class
        self.context = context
        self.translator_class = translator_class

        self.op, self.result_handler = self._adapt_operation(node)
        assert isinstance(self.op, ops.Node), type(self.op)

        self.table_set = None
        self.select_set = None
        self.group_by = None
        self.having = None
        self.filters = []
        self.limit = None
        self.sort_by = []
        self.subqueries = []
        self.distinct = False

        select_query = self._build_result_query()

        self.queries = [select_query]

        return select_query

    @staticmethod
    def _foreign_ref_check(query, expr):
        checker = _CorrelatedRefCheck(query, expr)
        return checker.get_result()

    @staticmethod
    def _adapt_operation(node):
        # Non-table expressions need to be adapted to some well-formed table
        # expression, along with a way to adapt the results to the desired
        # arity (whether array-like or scalar, for example)
        #
        # Canonical case is scalar values or arrays produced by some reductions
        # (simple reductions, or distinct, say)
        if isinstance(node, ops.TableNode):
            return node, toolz.identity

        elif isinstance(node, ops.Value):
            if node.output_shape is Shape.SCALAR:
                if L.is_scalar_reduction(node):
                    table_expr = L.reduction_to_aggregation(node)
                    return table_expr.op(), _get_scalar(node.name)
                else:
                    return node, _get_scalar(node.name)
            elif node.output_shape is Shape.COLUMNAR:
                if isinstance(node, ops.TableColumn):
                    table_expr = node.table.to_expr()[[node.name]]
                    result_handler = _get_column(node.name)
                else:
                    table_expr = node.to_expr().to_projection()
                    result_handler = _get_column(node.name)

                return table_expr.op(), result_handler
            else:
                raise com.TranslationError(
                    f"Unexpected shape {node.output_shape}"
                )

        elif isinstance(node, (ops.Analytic, ops.TopK)):
            return node.to_expr().to_aggregation().op(), toolz.identity

        else:
            raise com.TranslationError(
                f'Do not know how to execute: {type(node)}'
            )

    def _build_result_query(self):
        self._collect_elements()
        self._analyze_select_exprs()
        self._analyze_subqueries()
        self._populate_context()

        return self.select_class(
            self.table_set,
            self.select_set,
            translator_class=self.translator_class,
            table_set_formatter_class=self.table_set_formatter_class,
            context=self.context,
            subqueries=self.subqueries,
            where=self.filters,
            group_by=self.group_by,
            having=self.having,
            limit=self.limit,
            order_by=self.sort_by,
            distinct=self.distinct,
            result_handler=self.result_handler,
            parent_op=self.op,
        )

    def _populate_context(self):
        # Populate aliases for the distinct relations used to output this
        # select statement.
        if self.table_set is not None:
            self._make_table_aliases(self.table_set)

        # XXX: This is a temporary solution to the table-aliasing / correlated
        # subquery problem. Will need to revisit and come up with a cleaner
        # design (also as one way to avoid pathological naming conflicts; for
        # example, we could define a table alias before we know that it
        # conflicts with the name of a table used in a subquery, join, or
        # another part of the query structure)

        # There may be correlated subqueries inside the filters, requiring that
        # we use an explicit alias when outputting as SQL. For now, we're just
        # going to see if any table nodes appearing in the where stack have
        # been marked previously by the above code.
        for expr in self.filters:
            needs_alias = self._foreign_ref_check(self, expr)
            if needs_alias:
                self.context.set_always_alias()

    # TODO(kszucs): should be rewritten using lin.traverse()
    def _make_table_aliases(self, node):
        ctx = self.context

        if isinstance(node, ops.Join):
            for arg in node.args:
                if isinstance(arg, ops.TableNode):
                    self._make_table_aliases(arg)
        elif not ctx.is_extracted(node):
            ctx.make_alias(node)
        else:
            # The compiler will apply a prefix only if the current context
            # contains two or more table references. So, if we've extracted
            # a subquery into a CTE, we need to propagate that reference
            # down to child contexts so that they aren't missing any refs.
            ctx.set_ref(node, ctx.top_context.get_ref(node))

    # ---------------------------------------------------------------------
    # Expr analysis / rewrites

    def _analyze_select_exprs(self):
        new_select_set = []

        for op in self.select_set:
            new_op = self._visit_select_expr(op)
            new_select_set.append(new_op)

        self.select_set = new_select_set

    # TODO(kszucs): this should be rewritten using analysis.substitute()
    def _visit_select_expr(self, op):
        method = f'_visit_select_{type(op).__name__}'
        if hasattr(self, method):
            f = getattr(self, method)
            return f(op)
        elif isinstance(op, ops.Value):
            new_args = []
            for arg in op.args:
                if isinstance(arg, ops.Node):
                    arg = self._visit_select_expr(arg)
                new_args.append(arg)

            return type(op)(*new_args)
        else:
            return op

    # TODO(kszucs): avoid roundtripping between extpressions and operations
    def _visit_select_Histogram(self, op):
        assert isinstance(op, ops.Node), type(op)
        EPS = 1e-13

        if op.binwidth is None or op.base is None:
            aux_hash = op.aux_hash or util.guid()[:6]
            min_name = 'min_%s' % aux_hash
            max_name = 'max_%s' % aux_hash

            minmax = self.table_set.to_expr().aggregate(
                [
                    op.arg.to_expr().min().name(min_name),
                    op.arg.to_expr().max().name(max_name),
                ]
            )
            self.table_set = self.table_set.to_expr().cross_join(minmax).op()

            if op.base is None:
                base = minmax[min_name] - EPS
            else:
                base = op.base.to_expr()

            binwidth = (minmax[max_name] - base) / (op.nbins - 1)
        else:
            # Have both a bin width and a base
            binwidth = op.binwidth.to_expr()
            base = op.base.to_expr()

        bucket = ((op.arg.to_expr() - base) / binwidth).floor()
        if isinstance(op, ops.Named):
            bucket = bucket.name(op.name)

        return bucket.op()

    # ---------------------------------------------------------------------
    # Analysis of table set

    def _collect_elements(self):
        # If expr is a Value, we must seek out the Tables that it
        # references, build their ASTs, and mark them in our QueryContext

        # For now, we need to make the simplifying assumption that a value
        # expression that is being translated only depends on a single table
        # expression.

        if isinstance(self.op, ops.TableNode):
            self._collect(self.op, toplevel=True)
            assert self.table_set is not None
        else:
            self.select_set = [self.op]

    def _collect(self, op, toplevel=False):
        method = f'_collect_{type(op).__name__}'

        if hasattr(self, method):
            f = getattr(self, method)
            f(op, toplevel=toplevel)
        elif isinstance(op, (ops.PhysicalTable, ops.SQLQueryResult)):
            self._collect_PhysicalTable(op, toplevel=toplevel)
        elif isinstance(op, ops.Join):
            self._collect_Join(op, toplevel=toplevel)
        else:
            raise NotImplementedError(type(op))

    def _collect_Distinct(self, op, toplevel=False):
        if toplevel:
            self.distinct = True

        self._collect(op.table, toplevel=toplevel)

    def _collect_DropNa(self, op, toplevel=False):
        if toplevel:
            if op.subset is None:
                columns = [
                    ops.TableColumn(op.table, name)
                    for name in op.table.schema.names
                ]
            else:
                columns = op.subset
            if columns:
                filters = [
                    functools.reduce(
                        ops.And if op.how == "any" else ops.Or,
                        [ops.NotNull(c) for c in columns],
                    )
                ]
            elif op.how == "all":
                filters = [ops.Literal(False, dtype=dt.bool)]
            else:
                filters = []
            self.table_set = op.table
            self.select_set = [op.table]
            self.filters = filters

    def _collect_Limit(self, op, toplevel=False):
        if not toplevel:
            return

        n = op.n
        offset = op.offset or 0

        if self.limit is None:
            self.limit = _LimitSpec(n, offset)
        else:
            self.limit = _LimitSpec(
                min(n, self.limit.n),
                offset + self.limit.offset,
            )

        self._collect(op.table, toplevel=toplevel)

    def _collect_Union(self, op, toplevel=False):
        if toplevel:
            raise NotImplementedError()

    def _collect_Difference(self, op, toplevel=False):
        if toplevel:
            raise NotImplementedError()

    def _collect_Intersection(self, op, toplevel=False):
        if toplevel:
            raise NotImplementedError()

    def _collect_Aggregation(self, op, toplevel=False):
        # The select set includes the grouping keys (if any), and these are
        # duplicated in the group_by set. SQL translator can decide how to
        # format these depending on the database. Most likely the
        # GROUP BY 1, 2, ... style
        if toplevel:
            sub_op = L.substitute_parents(op)

            self.group_by = self._convert_group_by(sub_op.by)
            self.having = sub_op.having
            self.select_set = sub_op.by + sub_op.metrics
            self.table_set = sub_op.table
            self.filters = sub_op.predicates
            self.sort_by = sub_op.sort_keys

            self._collect(op.table)

    def _collect_Selection(self, op, toplevel=False):
        table = op.table

        if toplevel:
            if isinstance(table, ops.Join):
                self._collect_Join(table)
            else:
                self._collect(table)

            selections = op.selections
            sort_keys = op.sort_keys
            filters = op.predicates

            if not selections:
                # select *
                selections = [table]

            self.sort_by = sort_keys
            self.select_set = selections
            self.table_set = table
            self.filters = filters

    def _collect_PandasInMemoryTable(self, node, toplevel=False):
        if toplevel:
            self.select_set = [node]
            self.table_set = node

    def _convert_group_by(self, nodes):
        return list(range(len(nodes)))

    def _collect_Join(self, op, toplevel=False):
        if toplevel:
            subbed = L.substitute_parents(op)
            self.table_set = subbed
            self.select_set = [subbed]

    def _collect_PhysicalTable(self, op, toplevel=False):
        if toplevel:
            self.select_set = [op]
            self.table_set = op

    def _collect_SelfReference(self, op, toplevel=False):
        if toplevel:
            self._collect(op.table, toplevel=toplevel)

    # --------------------------------------------------------------------
    # Subquery analysis / extraction

    def _analyze_subqueries(self):
        # Somewhat temporary place for this. A little bit tricky, because
        # subqueries can be found in many places
        # - With the table set
        # - Inside the where clause (these may be able to place directly, some
        #   cases not)
        # - As support queries inside certain expressions (possibly needing to
        #   be extracted and joined into the table set where they are
        #   used). More complex transformations should probably not occur here,
        #   though.
        #
        # Duplicate subqueries might appear in different parts of the query
        # structure, e.g. beneath two aggregates that are joined together, so
        # we have to walk the entire query structure.
        #
        # The default behavior is to only extract into a WITH clause when a
        # subquery appears multiple times (for DRY reasons). At some point we
        # can implement a more aggressive policy so that subqueries always
        # appear in the WITH part of the SELECT statement, if that's what you
        # want.

        # Find the subqueries, and record them in the passed query context.
        subqueries = _extract_common_table_expressions(
            [self.table_set, *self.filters]
        )

        self.subqueries = []
        for node in subqueries:
            # See #173. Might have been extracted already in a parent context.
            if not self.context.is_extracted(node):
                self.subqueries.append(node)
                self.context.set_extracted(node)
