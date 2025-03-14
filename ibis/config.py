from __future__ import annotations

import contextlib
from typing import Any, Callable, Optional

from public import public
from typing_extensions import Annotated

from ibis.common.grounds import Annotable
from ibis.common.validators import min_

PosInt = Annotated[int, min_(0)]


class Config(Annotable):
    def get(self, key: str) -> Any:
        value = self
        for field in key.split("."):
            value = getattr(value, field)
        return value

    def set(self, key: str, value: Any) -> None:
        *prefix, key = key.split(".")
        conf = self
        for field in prefix:
            conf = getattr(conf, field)
        setattr(conf, key, value)

    @contextlib.contextmanager
    def _with_temporary(self, options):
        try:
            old = {}
            for key, value in options.items():
                old[key] = self.get(key)
                self.set(key, value)
            yield
        finally:
            for key, value in old.items():
                self.set(key, value)

    def __call__(self, options):
        return self._with_temporary(options)


class ContextAdjustment(Config):
    """Options related to time context adjustment.

    Attributes
    ----------
    time_col : str
        Name of the timestamp column for execution with a `timecontext`. See
        `ibis/expr/timecontext.py` for details.
    """

    time_col: str = "time"


class SQL(Config):
    """SQL-related options.

    Attributes
    ----------
    default_limit : int | None
        Number of rows to be retrieved for a table expression without an
        explicit limit. [`None`][None] means no limit.
    default_dialect : str
        Dialect to use for printing SQL when the backend cannot be determined.
    """

    default_limit: Optional[PosInt] = 10_000
    default_dialect: str = "duckdb"


class Interactive(Config):
    """Options controlling the interactive repr.

    Attributes
    ----------
    max_rows : int
        Maximum rows to pretty print.
    max_length : int
        Maximum length for pretty-printed arrays and maps.
    max_string : int
        Maximum length for pretty-printed strings.
    max_depth : int
        Maximum depth for nested data types.
    show_types : bool
        Show the inferred type of value expressions in the interactive repr.
    """

    max_rows: int = 10
    max_length: int = 5
    max_string: int = 80
    max_depth: int = 2
    show_types: bool = True


class Repr(Config):
    """Expression printing options.

    Attributes
    ----------
    depth : int
        The maximum number of expression nodes to print when repring.
    table_columns : int
        The number of columns to show in leaf table expressions.
    query_text_length : int
        The maximum number of characters to show in the `query` field repr of
        SQLQueryResult operations.
    show_types : bool
        Show the inferred type of value expressions in the repr.
    interactive
        Options controlling the interactive repr.
    """

    depth: Optional[PosInt] = None
    table_columns: Optional[PosInt] = None
    query_text_length: PosInt = 80
    show_types: bool = False
    interactive: Interactive = Interactive()


class Options(Config):
    """Ibis configuration options.

    Attributes
    ----------
    interactive : bool
        Show the first few rows of computing an expression when in a repl.
    repr : Repr
        Options controlling expression printing.
    verbose : bool
        Run in verbose mode if [`True`][True]
    verbose_log: Callable[[str], None] | None
        A callable to use when logging.
    graphviz_repr : bool
        Render expressions as GraphViz PNGs when running in a Jupyter notebook.
    default_backend : Optional[str], default None
        The default backend to use for execution, defaults to DuckDB if not
        set.
    context_adjustment : ContextAdjustment
        Options related to time context adjustment.
    sql: SQL
        SQL-related options.
    clickhouse : Config | None
        Clickhouse specific options.
    dask : Config | None
        Dask specific options.
    impala : Config | None
        Impala specific options.
    pandas : Config | None
        Pandas specific options.
    pyspark : Config | None
        PySpark specific options.
    """

    interactive: bool = False
    repr: Repr = Repr()
    verbose: bool = False
    verbose_log: Optional[Callable] = None
    graphviz_repr: bool = False
    default_backend: Optional[Any] = None
    context_adjustment: ContextAdjustment = ContextAdjustment()
    sql: SQL = SQL()
    clickhouse: Optional[Config] = None
    dask: Optional[Config] = None
    impala: Optional[Config] = None
    pandas: Optional[Config] = None
    pyspark: Optional[Config] = None


_HAS_DUCKDB = True
_DUCKDB_CON = None


def _default_backend() -> Any:
    global _HAS_DUCKDB, _DUCKDB_CON

    if not _HAS_DUCKDB:
        return None

    if _DUCKDB_CON is not None:
        return _DUCKDB_CON

    try:
        import duckdb as _  # noqa: F401
    except ImportError:
        _HAS_DUCKDB = False
        return None

    import ibis

    _DUCKDB_CON = ibis.duckdb.connect(":memory:")
    return _DUCKDB_CON


options = Options()


@public
def option_context(key, new_value):
    return options({key: new_value})


public(options=options)
