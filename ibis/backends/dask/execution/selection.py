"""Dispatching code for Selection operations."""


import functools
import operator
from typing import List, Optional

import dask.dataframe as dd
import pandas
from toolz import concatv

import ibis.expr.analysis as an
import ibis.expr.operations as ops
from ibis.backends.dask.core import execute
from ibis.backends.dask.dispatch import execute_node
from ibis.backends.dask.execution.util import (
    add_partitioned_sorted_column,
    coerce_to_output,
    compute_sorted_frame,
    is_row_order_preserving,
)
from ibis.backends.pandas.execution.selection import (
    build_df_from_selection,
    map_new_column_names_to_data,
    remap_overlapping_column_names,
)
from ibis.backends.pandas.execution.util import get_join_suffix_for_op
from ibis.expr.rules import Shape
from ibis.expr.scope import Scope
from ibis.expr.typing import TimeContext


# TODO(kszucs): deduplicate with pandas.compute_projection() since it is almost
# an exact copy of that function
def compute_projection(
    node,
    parent,
    data,
    scope: Scope = None,
    timecontext: Optional[TimeContext] = None,
    **kwargs,
):
    """Compute a projection.

    Parameters
    ----------
    node : Union[ops.Scalar, ops.Column, ops.TableNode]
    parent : ops.Selection
    data : pd.DataFrame
    scope : Scope
    timecontext:Optional[TimeContext]

    Returns
    -------
    value : scalar, pd.Series, pd.DataFrame

    Notes
    -----
    :class:`~ibis.expr.types.Scalar` instances occur when a specific column
    projection is a window operation.
    """
    if isinstance(node, ops.TableNode):
        if node == parent.table:
            return data

        assert isinstance(parent.table, ops.Join)
        assert node == parent.table.left or node == parent.table.right

        mapping = remap_overlapping_column_names(
            parent.table,
            root_table=node,
            data_columns=frozenset(data.columns),
        )
        return map_new_column_names_to_data(mapping, data)
    elif isinstance(node, ops.Value):
        name = node.name
        assert name is not None, 'Value selection name is None'

        if node.output_shape is Shape.SCALAR:
            data_columns = frozenset(data.columns)

            if scope is None:
                scope = Scope()

            scope = scope.merge_scopes(
                Scope(
                    {
                        t: map_new_column_names_to_data(
                            remap_overlapping_column_names(
                                parent.table, t, data_columns
                            ),
                            data,
                        )
                    },
                    timecontext,
                )
                for t in an.find_immediate_parent_tables(node)
            )
            scalar = execute(node, scope=scope, **kwargs)
            return data.assign(**{name: scalar})[name]
        else:
            if isinstance(node, ops.TableColumn):
                if name in data:
                    return data[name].rename(name)

                if not isinstance(parent.table, ops.Join):
                    raise KeyError(name)

                suffix = get_join_suffix_for_op(node, parent.table)
                return data.loc[:, name + suffix].rename(name)

            data_columns = frozenset(data.columns)

            scope = scope.merge_scopes(
                Scope(
                    {
                        t: map_new_column_names_to_data(
                            remap_overlapping_column_names(
                                parent.table, t, data_columns
                            ),
                            data,
                        )
                    },
                    timecontext,
                )
                for t in an.find_immediate_parent_tables(node)
            )

            result = execute(
                node, scope=scope, timecontext=timecontext, **kwargs
            )
            return coerce_to_output(result, node, data.index)
    else:
        raise TypeError(node)


def build_df_from_projection(
    selections: List[ops.Node],
    op: ops.Selection,
    data: dd.DataFrame,
    **kwargs,
) -> dd.DataFrame:
    """Build up a df from individual pieces by dispatching to
    `compute_projection` for each expression."""
    # Fast path for when we're assigning columns into the same table.
    if (selections[0] is op.table) and all(
        is_row_order_preserving(selections[1:])
    ):
        for node in selections[1:]:
            projection = compute_projection(node, op, data, **kwargs)
            if isinstance(projection, dd.Series):
                data = data.assign(**{projection.name: projection})
            else:
                data = data.assign(
                    **{c: projection[c] for c in projection.columns}
                )
        return data

    # Slow path when we cannot do direct assigns
    # Create a unique row identifier and set it as the index. This is
    # used in dd.concat to merge the pieces back together.
    data = add_partitioned_sorted_column(data)
    data_pieces = [
        compute_projection(node, op, data, **kwargs) for node in selections
    ]

    return dd.concat(data_pieces, axis=1).reset_index(drop=True)


@execute_node.register(ops.Selection, dd.DataFrame)
def execute_selection_dataframe(
    op,
    data,
    scope: Scope,
    timecontext: Optional[TimeContext],
    **kwargs,
):
    result = data

    if op.predicates:
        predicates = _compute_predicates(
            op.table, op.predicates, data, scope, timecontext, **kwargs
        )
        predicate = functools.reduce(operator.and_, predicates)
        result = result.loc[predicate]

    if op.selections:
        # if we are just performing select operations we can do a direct
        # selection
        if all(isinstance(s, ops.TableColumn) for s in op.selections):
            result = build_df_from_selection(op.selections, result, op.table)
        else:
            result = build_df_from_projection(
                op.selections,
                op,
                result,
                scope=scope,
                timecontext=timecontext,
                **kwargs,
            )

    if op.sort_keys:
        if len(op.sort_keys) > 1:
            raise NotImplementedError(
                """
                Multi-key sorting is not implemented for the Dask backend
                """
            )
        sort_key = op.sort_keys[0]
        if sort_key.descending:
            raise NotImplementedError(
                "Descending sort is not supported for the Dask backend"
            )
        result = compute_sorted_frame(
            result,
            order_by=sort_key,
            scope=scope,
            timecontext=timecontext,
            **kwargs,
        )

        return result
    else:
        grouping_keys = ordering_keys = ()

    # return early if we do not have any temporary grouping or ordering columns
    assert not grouping_keys, 'group by should never show up in Selection'
    if not ordering_keys:
        return result

    # create a sequence of columns that we need to drop
    temporary_columns = pandas.Index(
        concatv(grouping_keys, ordering_keys)
    ).difference(data.columns)

    # no reason to call drop if we don't need to
    if temporary_columns.empty:
        return result

    # drop every temporary column we created for ordering or grouping
    return result.drop(temporary_columns, axis=1)


def _compute_predicates(
    table_op,
    predicates,
    data,
    scope: Scope,
    timecontext: Optional[TimeContext],
    **kwargs,
):
    """Compute the predicates for a table operation.

    Parameters
    ----------
    table_op : TableNode
    predicates : List[ir.Column]
    data : pd.DataFrame
    scope : Scope
    timecontext: Optional[TimeContext]
    kwargs : dict

    Returns
    -------
    computed_predicate : pd.Series[bool]

    Notes
    -----
    This handles the cases where the predicates are computed columns, in
    addition to the simple case of named columns coming directly from the input
    table.
    """
    for predicate in predicates:
        # Map each root table of the predicate to the data so that we compute
        # predicates on the result instead of any left or right tables if the
        # Selection is on a Join. Project data to only inlude columns from
        # the root table.
        root_tables = an.find_immediate_parent_tables(predicate)

        # handle suffixes
        data_columns = frozenset(data.columns)

        additional_scope = Scope()
        for root_table in root_tables:
            mapping = remap_overlapping_column_names(
                table_op, root_table, data_columns
            )
            if mapping is not None:
                new_data = data.loc[:, mapping.keys()].rename(columns=mapping)
            else:
                new_data = data
            additional_scope = additional_scope.merge_scope(
                Scope({root_table: new_data}, timecontext)
            )

        scope = scope.merge_scope(additional_scope)
        yield execute(predicate, scope=scope, **kwargs)
