from __future__ import annotations

import datetime
import decimal
from typing import IO

import rich
from rich.console import Console

import ibis
import ibis.common.exceptions as com
import ibis.expr.datatypes as dt
import ibis.expr.types as ir

console = Console()

_IBIS_TO_SQLGLOT_NAME_MAP = {
    # not 100% accurate, but very close
    "impala": "hive",
    # for now map clickhouse to Hive so that _something_ works
    "clickhouse": "mysql",
}


def show_sql(
    expr: ir.Expr,
    dialect: str | None = None,
    file: IO[str] | None = None,
) -> None:
    """Pretty-print the compiled SQL string of an expression.

    If a dialect cannot be inferred and one was not passed, duckdb
    will be used as the dialect

    Parameters
    ----------
    expr
        Ibis expression whose SQL will be printed
    dialect
        String dialect. This is typically not required, but can be useful if
        ibis cannot infer the backend dialect.
    file
        File to write output to

    Examples
    --------
    >>> import ibis
    >>> from ibis import _
    >>> t = ibis.table(dict(a="int"), name="t")
    >>> expr = t.select(c=_.a * 2)
    >>> ibis.show_sql(expr)  # duckdb dialect by default
    SELECT
      t0.a * CAST(2 AS SMALLINT) AS c
    FROM t AS t0
    >>> ibis.show_sql(expr, dialect="mysql")
    SELECT
      t0.a * 2 AS c
    FROM t AS t0
    """
    print(to_sql(expr, dialect=dialect), file=file)


def to_sql(expr: ir.Expr, dialect: str | None = None) -> str:
    """Return the formatted SQL string for an expression.

    Parameters
    ----------
    expr
        Ibis expression.
    dialect
        SQL dialect to use for compilation.

    Returns
    -------
    str
        Formatted SQL string
    """
    import sqlglot

    # try to infer from a non-str expression or if not possible fallback to
    # the default pretty dialect for expressions
    if dialect is None:
        try:
            backend = expr._find_backend()
        except com.IbisError:
            # default to duckdb for sqlalchemy compilation because it supports
            # the widest array of ibis features for SQL backends
            read = "duckdb"
            write = ibis.options.sql.default_dialect
        else:
            read = write = backend.name
    else:
        read = write = dialect

    write = _IBIS_TO_SQLGLOT_NAME_MAP.get(write, write)

    try:
        compiled = expr.compile()
    except com.IbisError:
        backend = getattr(ibis, read)
        compiled = backend.compile(expr)
    try:
        sql = str(compiled.compile(compile_kwargs={"literal_binds": True}))
    except (AttributeError, TypeError):
        sql = compiled

    assert isinstance(
        sql, str
    ), f"expected `str`, got `{sql.__class__.__name__}`"
    (pretty,) = sqlglot.transpile(
        sql,
        read=_IBIS_TO_SQLGLOT_NAME_MAP.get(read, read),
        write=write,
        pretty=True,
    )
    return pretty


def _pretty_value(v, dtype: dt.DataType):
    if isinstance(v, str):
        return v

    if isinstance(v, decimal.Decimal):
        return f"[bold][light_salmon1]{v}[/][/]"

    if isinstance(v, datetime.datetime):
        if isinstance(dtype, dt.Date):
            return f"[magenta]{v.date()}[/]"

        fmt = v.isoformat(timespec='milliseconds').replace("T", " ")
        return f"[magenta]{fmt}[/]"

    if isinstance(v, datetime.timedelta):
        return f"[magenta]{v}[/]"

    interactive = ibis.options.repr.interactive
    return rich.pretty.Pretty(
        v,
        max_length=interactive.max_length,
        max_string=interactive.max_string,
        max_depth=interactive.max_depth,
    )


def _format_value(v) -> str:
    if v is None:
        # render NULL values as the empty set
        return "[dim][yellow]∅[/][/]"

    if isinstance(v, str):
        if not v:
            return "[dim][yellow]~[/][/]"

        v = (
            # replace spaces with dots
            v.replace(" ", "[dim]·[/]")
            # tab
            .replace("\t", r"[dim]\t[/]")
            # carriage return
            .replace("\r", r"[dim]\r[/]")
            # line feed
            .replace("\n", r"[dim]\n[/]")
            # vertical tab
            .replace("\v", r"[dim]\v[/]")
            # form feed (page break)
            .replace("\f", r"[dim]\f[/]")
        )
        # display all unprintable characters as a dimmed version of their repr
        return "".join(
            f"[dim]{repr(c)[1:-1]}[/]" if not c.isprintable() else c for c in v
        )
    return v


def _format_dtype(dtype):
    strtyp = str(dtype)
    max_string = ibis.options.repr.interactive.max_string
    return (
        ("[bold][dark_orange]![/][/]" * (not dtype.nullable))
        + "[bold][blue]"
        + (
            strtyp[(not dtype.nullable) : max_string]
            + "[orange1]…[/]" * (len(strtyp) > max_string)
        )
        + "[/][/]"
    )
