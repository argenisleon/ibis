from __future__ import annotations

import os
import webbrowser
from functools import cached_property
from typing import TYPE_CHECKING, Any, Mapping

import toolz
from public import public

import ibis.common.graph as g
import ibis.expr.operations as ops
from ibis.common.exceptions import (
    ExpressionError,
    IbisError,
    IbisTypeError,
    TranslationError,
)
from ibis.common.grounds import Immutable
from ibis.common.pretty import console
from ibis.config import _default_backend, options
from ibis.expr.typing import TimeContext
from ibis.util import UnnamedMarker

if TYPE_CHECKING:
    import ibis.expr.types as ir
    from ibis.backends.base import BaseBackend


# TODO(kszucs): consider to subclass from Annotable with a single _arg field
@public
class Expr(Immutable):
    """Base expression class."""

    __slots__ = ("_arg",)

    def __init__(self, arg: ops.Node) -> None:
        object.__setattr__(self, "_arg", arg)

    def __repr__(self) -> str:
        if not options.interactive:
            return self._repr()

        with console.capture() as capture:
            try:
                console.print(self)
            except TranslationError as e:
                lines = [
                    "Translation to backend failed",
                    f"Error message: {e.args[0]}",
                    "Expression repr follows:",
                    self._repr(),
                ]
                return "\n".join(lines)
        return capture.get()

    def __reduce__(self):
        return (self.__class__, (self._arg,))

    def __hash__(self):
        return hash((self.__class__, self._arg))

    def _repr(self) -> str:
        from ibis.expr.format import fmt

        return fmt(self)

    def equals(self, other):
        if not isinstance(other, Expr):
            raise TypeError(
                "invalid equality comparison between Expr and "
                f"{type(other)}"
            )
        return self._arg.equals(other._arg)

    def __bool__(self) -> bool:
        raise ValueError(
            "The truth value of an Ibis expression is not defined"
        )

    __nonzero__ = __bool__

    def has_name(self):
        return isinstance(self.op(), ops.Named)

    def get_name(self):
        return self.op().name

    # TODO(kszucs): remove it entirely
    @cached_property
    def _safe_name(self) -> str | None:
        """Get the name of an expression `expr` if one exists.

        Returns
        -------
        str | None
            `str` if the Expr has a name, otherwise `None`
        """
        try:
            return self.get_name()
        except (ExpressionError, AttributeError):
            return None

    def _repr_png_(self) -> bytes | None:
        if options.interactive or not options.graphviz_repr:
            return None
        try:
            import ibis.expr.visualize as viz
        except ImportError:
            return None
        else:
            try:
                return viz.to_graph(self).pipe(format='png')
            except Exception:
                # Something may go wrong, and we can't error in the notebook
                # so fallback to the default text representation.
                return None

    def visualize(
        self,
        format: str = "svg",
        *,
        label_edges: bool = False,
        verbose: bool = False,
    ) -> None:
        """Visualize an expression in the browser.

        Parameters
        ----------
        format
            Image output format. These are specified by the ``graphviz`` Python
            library.
        label_edges
            Show operation input names as edge labels
        verbose
            Print the graphviz DOT code to stderr if [`True`][True]

        Notes
        -----
        This method opens a web browser tab showing the image of the expression
        graph created by the code in [ibis.expr.visualize][].

        Raises
        ------
        ImportError
            If ``graphviz`` is not installed.
        """
        import ibis.expr.visualize as viz

        path = viz.draw(
            viz.to_graph(self, label_edges=label_edges),
            format=format,
            verbose=verbose,
        )
        webbrowser.open(f'file://{os.path.abspath(path)}')

    def pipe(self, f, *args: Any, **kwargs: Any) -> Expr:
        """Compose `f` with `self`.

        Parameters
        ----------
        f
            If the expression needs to be passed as anything other than the
            first argument to the function, pass a tuple with the argument
            name. For example, (f, 'data') if the function f expects a 'data'
            keyword
        args
            Positional arguments to `f`
        kwargs
            Keyword arguments to `f`

        Examples
        --------
        >>> import ibis
        >>> t = ibis.table([('a', 'int64'), ('b', 'string')], name='t')
        >>> f = lambda a: (a + 1).name('a')
        >>> g = lambda a: (a * 2).name('a')
        >>> result1 = t.a.pipe(f).pipe(g)
        >>> result1
        r0 := UnboundTable[t]
          a int64
          b string
        a: r0.a + 1 * 2

        >>> result2 = g(f(t.a))  # equivalent to the above
        >>> result1.equals(result2)
        True

        Returns
        -------
        Expr
            Result type of passed function
        """
        if isinstance(f, tuple):
            f, data_keyword = f
            kwargs = kwargs.copy()
            kwargs[data_keyword] = self
            return f(*args, **kwargs)
        else:
            return f(self, *args, **kwargs)

    def op(self) -> ops.Node:
        return self._arg

    def _find_backends(self) -> tuple[list[BaseBackend], bool]:
        """Return the possible backends for an expression.

        Returns
        -------
        list[BaseBackend]
            A list of the backends found.
        """
        import ibis.expr.operations as ops
        from ibis.backends.base import BaseBackend

        def finder(node):
            # BaseBackend objects are not operation instances, so they don't
            # get traversed, this is why we need to select backends out from
            # the node's arguments
            backends = [
                arg for arg in node.args if isinstance(arg, BaseBackend)
            ]
            return g.proceed, (backends, isinstance(node, ops.UnboundTable))

        all_backends = []
        any_unbound = False
        for backends, has_unbound in g.traverse(finder, self.op()):
            all_backends += backends
            any_unbound |= has_unbound

        return list(toolz.unique(all_backends)), any_unbound

    def _find_backend(self, *, use_default: bool = False) -> BaseBackend:
        """Find the backend attached to an expression.

        Parameters
        ----------
        use_default
            If [`True`][True] and the default backend isn't set, initialize the
            default backend and use that. This should only be set to `True` for
            `.execute()`. For other contexts such as compilation, this option
            doesn't make sense so the default value is [`False`][False].

        Returns
        -------
        BaseBackend
            A backend that is attached to the expression
        """
        backends, has_unbound = self._find_backends()

        if not backends:
            if has_unbound:
                raise IbisError(
                    "Expression contains unbound tables and therefore cannot "
                    "be executed. Use ibis.<backend>.execute(expr) or "
                    "assign a backend instance to "
                    "`ibis.options.default_backend`."
                )
            if (default := options.default_backend) is None and use_default:
                default = _default_backend()
            if default is None:
                raise IbisError(
                    'Expression depends on no backends, and found no default'
                )
            return default

        if len(backends) > 1:
            raise ValueError('Multiple backends found')

        return backends[0]

    def execute(
        self,
        limit: int | str | None = 'default',
        timecontext: TimeContext | None = None,
        params: Mapping[ir.Value, Any] | None = None,
        **kwargs: Any,
    ):
        """Execute an expression against its backend if one exists.

        Parameters
        ----------
        limit
            An integer to effect a specific row limit. A value of `None` means
            "no limit". The default is in `ibis/config.py`.
        timecontext
            Defines a time range of `(begin, end)`. When defined, the execution
            will only compute result for data inside the time range. The time
            range is inclusive of both endpoints. This is conceptually same as
            a time filter.
            The time column must be named `'time'` and should preserve
            across the expression. For example, if that column is dropped then
            execute will result in an error.
        params
            Mapping of scalar parameter expressions to value
        """
        return self._find_backend(use_default=True).execute(
            self, limit=limit, timecontext=timecontext, params=params, **kwargs
        )

    def compile(
        self,
        limit: int | None = None,
        timecontext: TimeContext | None = None,
        params: Mapping[ir.Value, Any] | None = None,
    ):
        """Compile to an execution target.

        Parameters
        ----------
        limit
            An integer to effect a specific row limit. A value of `None` means
            "no limit". The default is in `ibis/config.py`.
        timecontext
            Defines a time range of `(begin, end)`. When defined, the execution
            will only compute result for data inside the time range. The time
            range is inclusive of both endpoints. This is conceptually same as
            a time filter.
            The time column must be named `'time'` and should preserve
            across the expression. For example, if that column is dropped then
            execute will result in an error.
        params
            Mapping of scalar parameter expressions to value
        """
        return self._find_backend().compile(
            self, limit=limit, timecontext=timecontext, params=params
        )


unnamed = UnnamedMarker()


def _binop(
    op_class: type[ops.Binary], left: ir.Value, right: ir.Value
) -> ir.Value | NotImplemented:
    """Try to construct a binary operation.

    Parameters
    ----------
    op_class
        The [`Binary`][ibis.expr.operations.Binary] subclass for the
        operation
    left
        Left operand
    right
        Right operand

    Returns
    -------
    ir.Value
        A value expression

    Examples
    --------
    >>> import ibis.expr.operations as ops
    >>> expr = _binop(ops.TimeAdd, ibis.time("01:00"), ibis.interval(hours=1))
    >>> expr
    datetime.time(1, 0) + 1
    >>> _binop(ops.TimeAdd, 1, ibis.interval(hours=1))
    NotImplemented
    """
    try:
        node = op_class(left, right)
    except (IbisTypeError, NotImplementedError):
        return NotImplemented
    else:
        return node.to_expr()
