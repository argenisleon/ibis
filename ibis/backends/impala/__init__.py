"""Impala backend."""

from __future__ import annotations

import contextlib
import io
import operator
import os
import re
import weakref
from pathlib import Path
from posixpath import join as pjoin
from typing import Any, Literal

import fsspec
import numpy as np
import pandas as pd

import ibis.common.exceptions as com
import ibis.config
import ibis.expr.datatypes as dt
import ibis.expr.rules as rlz
import ibis.expr.schema as sch
import ibis.util as util
from ibis.backends.base.sql import BaseSQLBackend
from ibis.backends.base.sql.ddl import (
    CTAS,
    CreateDatabase,
    CreateTableWithSchema,
    CreateView,
    DropDatabase,
    DropTable,
    DropView,
    TruncateTable,
    fully_qualified_re,
    is_fully_qualified,
)
from ibis.backends.impala import ddl, udf
from ibis.backends.impala.client import (
    ImpalaConnection,
    ImpalaDatabase,
    ImpalaTable,
)
from ibis.backends.impala.compat import HS2Error, ImpylaError
from ibis.backends.impala.compiler import ImpalaCompiler
from ibis.backends.impala.pandas_interop import DataFrameWriter
from ibis.backends.impala.udf import (  # noqa F408
    aggregate_function,
    scalar_function,
    wrap_uda,
    wrap_udf,
)
from ibis.config import options

_HS2_TTypeId_to_dtype = {
    'BOOLEAN': 'bool',
    'TINYINT': 'int8',
    'SMALLINT': 'int16',
    'INT': 'int32',
    'BIGINT': 'int64',
    'TIMESTAMP': 'datetime64[ns]',
    'FLOAT': 'float32',
    'DOUBLE': 'float64',
    'STRING': 'object',
    'DECIMAL': 'object',
    'BINARY': 'object',
    'VARCHAR': 'object',
    'CHAR': 'object',
    'DATE': 'datetime64[ns]',
    'VOID': None,
}


def _split_signature(x):
    name, rest = x.split('(', 1)
    return name, rest[:-1]


_arg_type = re.compile(r'(.*)\.\.\.|([^\.]*)')


class _type_parser:

    NORMAL, IN_PAREN = 0, 1

    def __init__(self, value):
        self.value = value
        self.state = self.NORMAL
        self.buf = io.StringIO()
        self.types = []
        for c in value:
            self._step(c)
        self._push()

    def _push(self):
        val = self.buf.getvalue().strip()
        if val:
            self.types.append(val)
        self.buf = io.StringIO()

    def _step(self, c):
        if self.state == self.NORMAL:
            if c == '(':
                self.state = self.IN_PAREN
            elif c == ',':
                self._push()
                return
        elif self.state == self.IN_PAREN:
            if c == ')':
                self.state = self.NORMAL
        self.buf.write(c)


def _chunks_to_pandas_array(chunks):
    total_length = 0
    have_nulls = False
    for c in chunks:
        total_length += len(c)
        have_nulls = have_nulls or c.nulls.any()

    type_ = chunks[0].data_type
    numpy_type = _HS2_TTypeId_to_dtype[type_]

    def fill_nonnull(target, chunks):
        pos = 0
        for c in chunks:
            target[pos : pos + len(c)] = c.values
            pos += len(c.values)

    def fill(target, chunks, na_rep):
        pos = 0
        for c in chunks:
            nulls = c.nulls.copy()
            nulls.bytereverse()
            bits = np.frombuffer(nulls.tobytes(), dtype='u1')
            mask = np.unpackbits(bits).view(np.bool_)

            k = len(c)

            dest = target[pos : pos + k]
            dest[:] = c.values
            dest[mask[:k]] = na_rep

            pos += k

    if have_nulls:
        if numpy_type in ('bool', 'datetime64[ns]'):
            target = np.empty(total_length, dtype='O')
            na_rep = np.nan
        elif numpy_type.startswith('int'):
            target = np.empty(total_length, dtype='f8')
            na_rep = np.nan
        else:
            target = np.empty(total_length, dtype=numpy_type)
            na_rep = np.nan

        fill(target, chunks, na_rep)
    else:
        target = np.empty(total_length, dtype=numpy_type)
        fill_nonnull(target, chunks)

    return target


def _column_batches_to_dataframe(names, batches):
    cols = {}
    for name, chunks in zip(names, zip(*(b.columns for b in batches))):
        cols[name] = _chunks_to_pandas_array(chunks)
    return pd.DataFrame(cols, columns=names)


class Backend(BaseSQLBackend):
    name = 'impala'
    database_class = ImpalaDatabase
    table_expr_class = ImpalaTable
    compiler = ImpalaCompiler

    class Options(ibis.config.Config):
        """Impala specific options.

        Parameters
        ----------
        temp_db : str, default "__ibis_tmp"
            Database to use for temporary objects.
        temp_hdfs_path : str, default "/tmp/ibis"
            HDFS path for storage of temporary data.
        """

        temp_db: str = "__ibis_tmp"
        temp_hdfs_path: str = "/tmp/hdfs"

    @staticmethod
    def hdfs_connect(
        *args: Any,
        protocol: str = "webhdfs",
        **kwargs: Any,
    ) -> fsspec.spec.AbstractFileSystem:
        return fsspec.filesystem(protocol, *args, **kwargs)

    def do_connect(
        self,
        host: str = "localhost",
        port: int = 21050,
        database: str = "default",
        timeout: int = 45,
        use_ssl: bool = False,
        ca_cert: str | Path | None = None,
        user: str | None = None,
        password: str | None = None,
        auth_mechanism: Literal[
            "NOSASL", "PLAIN", "GSSAPI", "LDAP"
        ] = "NOSASL",
        kerberos_service_name: str = "impala",
        pool_size: int = 8,
        hdfs_client: fsspec.spec.AbstractFileSystem | None = None,
    ):
        """Create an Impala Backend for use with Ibis.

        Parameters
        ----------
        host
            Host name of the impalad or HiveServer2 in Hive
        port
            Impala's HiveServer2 port
        database
            Default database when obtaining new cursors
        timeout
            Connection timeout in seconds when communicating with HiveServer2
        use_ssl
            Use SSL when connecting to HiveServer2
        ca_cert
            Local path to 3rd party CA certificate or copy of server
            certificate for self-signed certificates. If SSL is enabled, but
            this argument is ``None``, then certificate validation is skipped.
        user
            LDAP user to authenticate
        password
            LDAP password to authenticate
        auth_mechanism
            |   Value    | Meaning                        |
            | :--------: | :----------------------------- |
            | `'NOSASL'` | insecure Impala connections    |
            | `'PLAIN'`  | insecure Hive clusters         |
            |  `'LDAP'`  | LDAP authenticated connections |
            | `'GSSAPI'` | Kerberos-secured clusters      |
        kerberos_service_name
            Specify a particular `impalad` service principal.

        Examples
        --------
        >>> import os
        >>> import ibis
        >>> hdfs_host = os.environ.get('IBIS_TEST_NN_HOST', 'localhost')
        >>> hdfs_port = int(os.environ.get('IBIS_TEST_NN_PORT', 50070))
        >>> impala_host = os.environ.get('IBIS_TEST_IMPALA_HOST', 'localhost')
        >>> impala_port = int(os.environ.get('IBIS_TEST_IMPALA_PORT', 21050))
        >>> hdfs = ibis.impala.hdfs_connect(host=hdfs_host, port=hdfs_port)
        >>> client = ibis.impala.connect(
        ...     host=impala_host,
        ...     port=impala_port,
        ...     hdfs_client=hdfs,
        ... )
        >>> client  # doctest: +ELLIPSIS
        <ibis.backends.impala.Backend object at 0x...>
        """
        self._temp_objects = set()
        self._hdfs = hdfs_client

        params = {
            'host': host,
            'port': port,
            'database': database,
            'timeout': timeout,
            'use_ssl': use_ssl,
            'ca_cert': str(ca_cert),
            'user': user,
            'password': password,
            'auth_mechanism': auth_mechanism,
            'kerberos_service_name': kerberos_service_name,
        }
        self.con = ImpalaConnection(pool_size=pool_size, **params)

        self._ensure_temp_db_exists()

    @property
    def version(self):
        cursor = self.raw_sql('select version()')
        result = cursor.fetchone()[0]
        cursor.release()
        return result

    def list_databases(self, like=None):
        cur = self.raw_sql('SHOW DATABASES')
        databases = self._get_list(cur)
        cur.release()
        return self._filter_with_like(databases, like)

    def list_tables(self, like=None, database=None):
        statement = 'SHOW TABLES'
        if database is not None:
            statement += f' IN {database}'
        if like:
            m = fully_qualified_re.match(like)
            if m:
                database, quoted, unquoted = m.groups()
                like = quoted or unquoted
                return self.list_tables(like=like, database=database)
            statement += f" LIKE '{like}'"

        return self._filter_with_like(
            [row[0] for row in self.raw_sql(statement).fetchall()]
        )

    def fetch_from_cursor(self, cursor, schema):
        batches = cursor.fetchall(columnar=True)
        names = [x[0] for x in cursor.description]
        df = _column_batches_to_dataframe(names, batches)
        if schema:
            return schema.apply_to(df)
        return df

    @property
    def hdfs(self):
        if self._hdfs is None:
            raise com.IbisError(
                'No HDFS connection; must pass connection '
                'using the hdfs_client argument to '
                'ibis.impala.connect'
            )
        return self._hdfs

    @property
    def kudu(self):
        raise NotImplementedError(
            "kudu support using kudu-python is no longer supported; "
            "use impala facilities to manage kudu tables; "
            "see https://kudu.apache.org/docs/kudu_impala_integration.html"
        )

    def close(self):
        """Close the connection and drop temporary objects."""
        while self._temp_objects:
            finalizer = self._temp_objects.pop()
            with contextlib.suppress(HS2Error):
                finalizer()

        self.con.close()

    def disable_codegen(self, disabled=True):
        """Turn off or on LLVM codegen in Impala query execution.

        Parameters
        ----------
        disabled
            To disable codegen, pass with no argument or True. To enable
            codegen, pass False.
        """
        self.con.disable_codegen(disabled)

    def _fully_qualified_name(self, name, database):
        if is_fully_qualified(name):
            return name

        database = database or self.current_database
        return f'{database}.`{name}`'

    def _get_list(self, cur):
        tuples = cur.fetchall()
        return list(map(operator.itemgetter(0), tuples))

    @util.deprecated(
        version='2.0',
        instead='use a new connection to the database',
    )
    def set_database(self, name):
        # XXX The parent `Client` has a generic method that calls this same
        # method in the backend. But for whatever reason calling this code from
        # that method doesn't seem to work. Maybe `con` is a copy?
        self.con.set_database(name)

    @property
    def current_database(self):
        # XXX The parent `Client` has a generic method that calls this same
        # method in the backend. But for whatever reason calling this code from
        # that method doesn't seem to work. Maybe `con` is a copy?
        return self.con.database

    def create_database(self, name, path=None, force=False):
        """Create a new Impala database.

        Parameters
        ----------
        name
            Database name
        path
            HDFS path where to store the database data; otherwise uses Impala
            default
        force
            Forcibly create the database
        """
        if path:
            # explicit mkdir ensures the user own the dir rather than impala,
            # which is easier for manual cleanup, if necessary
            self.hdfs.mkdir(path)
        statement = CreateDatabase(name, path=path, can_exist=force)
        return self.raw_sql(statement)

    def drop_database(self, name, force=False):
        """Drop an Impala database.

        Parameters
        ----------
        name
            Database name
        force
            If False and there are any tables in this database, raises an
            IntegrityError
        """
        if not force or name in self.list_databases():
            tables = self.list_tables(database=name)
            udfs = self.list_udfs(database=name)
            udas = self.list_udas(database=name)
        else:
            tables = []
            udfs = []
            udas = []
        if force:
            for table in tables:
                util.log('Dropping {}'.format(f'{name}.{table}'))
                self.drop_table_or_view(table, database=name)
            for func in udfs:
                util.log(f'Dropping function {func.name}({func.inputs})')
                self.drop_udf(
                    func.name,
                    input_types=func.inputs,
                    database=name,
                    force=True,
                )
            for func in udas:
                util.log(
                    'Dropping aggregate function {}({})'.format(
                        func.name, func.inputs
                    )
                )
                self.drop_uda(
                    func.name,
                    input_types=func.inputs,
                    database=name,
                    force=True,
                )
        else:
            if len(tables) > 0 or len(udfs) > 0 or len(udas) > 0:
                raise com.IntegrityError(
                    'Database {} must be empty before '
                    'being dropped, or set '
                    'force=True'.format(name)
                )
        statement = DropDatabase(name, must_exist=not force)
        return self.raw_sql(statement)

    def get_schema(
        self,
        table_name: str,
        database: str | None = None,
    ) -> sch.Schema:
        """Return a Schema object for the indicated table and database.

        Parameters
        ----------
        table_name
            Table name
        database
            Database name

        Returns
        -------
        Schema
            Ibis schema
        """
        qualified_name = self._fully_qualified_name(table_name, database)
        query = f'DESCRIBE {qualified_name}'

        # only pull out the first two columns which are names and types
        pairs = [row[:2] for row in self.con.fetchall(query)]

        names, types = zip(*pairs)
        ibis_types = [udf.parse_type(type.lower()) for type in types]
        return sch.Schema(names, ibis_types)

    @property
    def client_options(self):
        return self.con.options

    def get_options(self):
        """Return current query options for the Impala session."""
        return dict(row[:2] for row in self.con.fetchall("SET"))

    def set_options(self, options):
        self.con.set_options(options)

    def reset_options(self):
        # Must nuke all cursors
        raise NotImplementedError

    def set_compression_codec(self, codec):
        if codec is None:
            codec = 'none'
        else:
            codec = codec.lower()

        if codec not in ('none', 'gzip', 'snappy'):
            raise ValueError(f'Unknown codec: {codec}')

        self.set_options({'COMPRESSION_CODEC': codec})

    def create_view(self, name, expr, database=None):
        """Create an Impala view from a table expression.

        Parameters
        ----------
        name
            View name
        expr : ibis Table
            Ibis table expression
        database
            Database name
        """
        ast = self.compiler.to_ast(expr)
        select = ast.queries[0]
        statement = CreateView(name, select, database=database)
        return self.raw_sql(statement)

    def drop_view(self, name, database=None, force=False):
        """Drop an Impala view.

        Parameters
        ----------
        name
            Table name
        database
            Database
        force
            Database may throw exception if table does not exist
        """
        statement = DropView(name, database=database, must_exist=not force)
        return self.raw_sql(statement)

    @contextlib.contextmanager
    def _setup_insert(self, obj):
        if isinstance(obj, pd.DataFrame):
            with DataFrameWriter(self, obj) as writer:
                yield writer.delimited_table(writer.write_temp_csv())
        else:
            yield obj

    def create_table(
        self,
        table_name,
        obj=None,
        schema=None,
        database=None,
        external=False,
        force=False,
        # HDFS options
        format='parquet',
        location=None,
        partition=None,
        like_parquet=None,
    ):
        """Create a new table in Impala using an Ibis table expression.

        This is currently designed for tables whose data is stored in HDFS.

        Parameters
        ----------
        table_name
            Table name
        obj
            If passed, creates table from select statement results
        schema
            Mutually exclusive with obj, creates an empty table with a
            particular schema
        database
            Database name
        force
            Do not create table if table with indicated name already exists
        external
            Create an external table; Impala will not delete the underlying
            data when the table is dropped
        format
            File format
        location
            Specify the directory location where Impala reads and writes files
            for the table
        partition
            Must pass a schema to use this. Cannot partition from an
            expression.
        like_parquet
            Can specify instead of a schema

        Examples
        --------
        >>> con.create_table('new_table_name', table_expr)  # doctest: +SKIP
        """
        if like_parquet is not None:
            raise NotImplementedError

        if obj is not None:
            with self._setup_insert(obj) as to_insert:
                ast = self.compiler.to_ast(to_insert)
                select = ast.queries[0]

                self.raw_sql(
                    CTAS(
                        table_name,
                        select,
                        database=database,
                        can_exist=force,
                        format=format,
                        external=external,
                        partition=partition,
                        path=location,
                    )
                )
        elif schema is not None:
            self.raw_sql(
                CreateTableWithSchema(
                    table_name,
                    schema,
                    database=database,
                    format=format,
                    can_exist=force,
                    external=external,
                    path=location,
                    partition=partition,
                )
            )
        else:
            raise com.IbisError('Must pass obj or schema')

    def avro_file(
        self,
        hdfs_dir,
        avro_schema,
        name=None,
        database=None,
        external=True,
        persist=False,
    ):
        """Create a table to read a collection of Avro data.

        Parameters
        ----------
        hdfs_dir
            Absolute HDFS path to directory containing avro files
        avro_schema
            The Avro schema for the data as a Python dict
        name
            Table name
        database
            Database name
        external
            Whether the table is external
        persist
            Persist the table

        Returns
        -------
        ImpalaTable
            Impala table expression
        """
        name, database = self._get_concrete_table_path(
            name, database, persist=persist
        )

        stmt = ddl.CreateTableAvro(
            name, hdfs_dir, avro_schema, database=database, external=external
        )
        self.raw_sql(stmt)
        return self._wrap_new_table(name, database, persist)

    def delimited_file(
        self,
        hdfs_dir,
        schema,
        name=None,
        database=None,
        delimiter=',',
        na_rep=None,
        escapechar=None,
        lineterminator=None,
        external=True,
        persist=False,
    ):
        """Interpret delimited text files as an Ibis table expression.

        See the `parquet_file` method for more details on what happens under
        the hood.

        Parameters
        ----------
        hdfs_dir
            HDFS directory containing delimited text files
        schema
            Ibis schema
        name
            Name for temporary or persistent table; otherwise random names are
            generated
        database
            Database to create the table in
        delimiter
            Character used to delimit columns
        escapechar
            Character used to escape special characters
        lineterminator
            Character used to delimit lines
        external
            Create table as EXTERNAL (data will not be deleted on drop). Not
            that if persist=False and external=False, whatever data you
            reference will be deleted
        persist
            If True, do not delete the table upon garbage collection of ibis
            table object

        Returns
        -------
        ImpalaTable
            Impala table expression
        """
        name, database = self._get_concrete_table_path(
            name, database, persist=persist
        )

        stmt = ddl.CreateTableDelimited(
            name,
            hdfs_dir,
            schema,
            database=database,
            delimiter=delimiter,
            external=external,
            na_rep=na_rep,
            lineterminator=lineterminator,
            escapechar=escapechar,
        )
        self.raw_sql(stmt)
        return self._wrap_new_table(name, database, persist)

    def parquet_file(
        self,
        hdfs_dir,
        schema=None,
        name=None,
        database=None,
        external=True,
        like_file=None,
        like_table=None,
        persist=False,
    ):
        """Make indicated parquet file in HDFS available as an Ibis table.

        The table created can be optionally named and persisted, otherwise a
        unique name will be generated. Temporarily, for any non-persistent
        external table created by Ibis we will attempt to drop it when the
        underlying object is garbage collected (or the Python interpreter shuts
        down normally).

        Parameters
        ----------
        hdfs_dir
            Path in HDFS
        schema
            If no schema provided, and neither of the like_* argument is
            passed, one will be inferred from one of the parquet files in the
            directory.
        like_file
            Absolute path to Parquet file in HDFS to use for schema
            definitions. An alternative to having to supply an explicit schema
        like_table
            Fully scoped and escaped string to an Impala table whose schema we
            will use for the newly created table.
        name
            Random unique name generated otherwise
        database
            Database to create the (possibly temporary) table in
        external
            If a table is external, the referenced data will not be deleted
            when the table is dropped in Impala. Otherwise (external=False)
            Impala takes ownership of the Parquet file.
        persist
            Do not drop the table during garbage collection

        Returns
        -------
        ImpalaTable
            Impala table expression
        """
        name, database = self._get_concrete_table_path(
            name, database, persist=persist
        )

        # If no schema provided, need to find some absolute path to a file in
        # the HDFS directory
        if like_file is None and like_table is None and schema is None:
            try:
                file_name = next(
                    fn
                    for fn in (
                        os.path.basename(f["name"])
                        for f in self.hdfs.ls(hdfs_dir, detail=True)
                        if f["type"].lower() == "file"
                    )
                    if not fn.startswith(("_", "."))
                    if not fn.endswith((".tmp", ".copying"))
                )
            except StopIteration:
                raise com.IbisError("No files found in the passed directory")
            else:
                like_file = pjoin(hdfs_dir, file_name)

        stmt = ddl.CreateTableParquet(
            name,
            hdfs_dir,
            schema=schema,
            database=database,
            example_file=like_file,
            example_table=like_table,
            external=external,
            can_exist=False,
        )
        self.raw_sql(stmt)
        return self._wrap_new_table(name, database, persist)

    def _get_concrete_table_path(self, name, database, persist=False):
        if not persist:
            if name is None:
                name = f'__ibis_tmp_{util.guid()}'

            if database is None:
                self._ensure_temp_db_exists()
                database = options.impala.temp_db
            return name, database
        else:
            if name is None:
                raise com.IbisError('Must pass table name if persist=True')
            return name, database

    def _ensure_temp_db_exists(self):
        # TODO: session memoize to avoid unnecessary `SHOW DATABASES` calls
        name, path = options.impala.temp_db, options.impala.temp_hdfs_path
        if name not in self.list_databases():
            if self._hdfs is not None:
                self.create_database(name, path=path, force=True)

    def _drop_table(self, name: str) -> None:
        # database might have been dropped, so we suppress the
        # corresponding Exception
        with contextlib.suppress(ImpylaError):
            self.drop_table(name)

    def _wrap_new_table(self, name, database, persist):
        qualified_name = self._fully_qualified_name(name, database)
        t = self.table(qualified_name)
        if not persist:
            self._temp_objects.add(
                # weakref the op instead of the expression because the table is
                # potentially collected after subsequent use when `_erase_expr`
                # unwraps the Expr layer
                weakref.finalize(t.op(), self._drop_table, qualified_name)
            )

        # Compute number of rows in table for better default query planning
        cardinality = t.count().execute()
        set_card = (
            "alter table {} set tblproperties('numRows'='{}', "
            "'STATS_GENERATED_VIA_STATS_TASK' = 'true')".format(
                qualified_name, cardinality
            )
        )
        self.raw_sql(set_card)

        return t

    def text_file(self, hdfs_path, column_name='value'):
        """Interpret text data as a table with a single string column."""

    def insert(
        self,
        table_name,
        obj=None,
        database=None,
        overwrite=False,
        partition=None,
        values=None,
        validate=True,
    ):
        """Insert data into an existing table.

        See
        [`ImpalaTable.insert`][ibis.backends.impala.client.ImpalaTable.insert]
        for parameters.

        Examples
        --------
        >>> table = 'my_table'
        >>> con.insert(table, table_expr)  # doctest: +SKIP

        Completely overwrite contents
        >>> con.insert(table, table_expr, overwrite=True)  # doctest: +SKIP
        """
        table = self.table(table_name, database=database)
        return table.insert(
            obj=obj,
            overwrite=overwrite,
            partition=partition,
            values=values,
            validate=validate,
        )

    def load_data(
        self,
        table_name,
        path,
        database=None,
        overwrite=False,
        partition=None,
    ):
        """Loads data into an Impala table by physically moving data files."""
        table = self.table(table_name, database=database)
        return table.load_data(path, overwrite=overwrite, partition=partition)

    def drop_table(self, table_name, database=None, force=False):
        """Drop an Impala table.

        Parameters
        ----------
        table_name
            Table name
        database
            Database name
        force
            Database may throw exception if table does not exist

        Examples
        --------
        >>> table = 'my_table'
        >>> db = 'operations'
        >>> con.drop_table(table, database=db, force=True)  # doctest: +SKIP
        """
        statement = DropTable(
            table_name, database=database, must_exist=not force
        )
        self.raw_sql(statement)

    def truncate_table(self, table_name, database=None):
        """Delete all rows from an existing table.

        Parameters
        ----------
        table_name
            Table name
        database
            Database name
        """
        statement = TruncateTable(table_name, database=database)
        self.raw_sql(statement)

    def drop_table_or_view(self, name, database=None, force=False):
        """Drop view or table."""
        try:
            self.drop_table(name, database=database)
        except Exception as e:
            try:
                self.drop_view(name, database=database)
            except Exception:
                raise e

    def cache_table(self, table_name, database=None, pool='default'):
        """Caches a table in cluster memory in the given pool.

        Parameters
        ----------
        table_name
            Table name
        database
            Database name
        pool
           The name of the pool in which to cache the table

        Examples
        --------
        >>> table = 'my_table'
        >>> db = 'operations'
        >>> pool = 'op_4GB_pool'
        >>> con.cache_table('my_table', database=db, pool=pool)  # doctest: +SKIP
        """  # noqa: E501
        statement = ddl.CacheTable(table_name, database=database, pool=pool)
        self.raw_sql(statement)

    def _get_schema_using_query(self, query):
        cur = self.raw_sql(query)
        # resets the state of the cursor and closes operation
        cur.fetchall()
        names, ibis_types = self._adapt_types(cur.description)
        cur.release()

        return sch.Schema(names, ibis_types)

    def create_function(self, func, name=None, database=None):
        """Create a function within Impala.

        Parameters
        ----------
        func
            UDF or UDAF
        name
            Function name
        database
            Database name
        """
        if name is None:
            name = func.name
        database = database or self.current_database

        if isinstance(func, udf.ImpalaUDF):
            stmt = ddl.CreateUDF(func, name=name, database=database)
        elif isinstance(func, udf.ImpalaUDA):
            stmt = ddl.CreateUDA(func, name=name, database=database)
        else:
            raise TypeError(func)
        self.raw_sql(stmt)

    def drop_udf(
        self,
        name,
        input_types=None,
        database=None,
        force=False,
        aggregate=False,
    ):
        """Drop a UDF.

        If only name is given, this will search for the relevant UDF and drop
        it. To delete an overloaded UDF, give only a name and force=True

        Parameters
        ----------
        name
            Function name
        input_types
            Input types
        force
            Must be set to `True` to drop overloaded UDFs
        database
            Database name
        aggregate
            Whether the function is an aggregate
        """
        if not input_types:
            if not database:
                database = self.current_database
            result = self.list_udfs(database=database, like=name)
            if len(result) > 1:
                if force:
                    for func in result:
                        self._drop_single_function(
                            func.name,
                            func.inputs,
                            database=database,
                            aggregate=aggregate,
                        )
                    return
                else:
                    raise Exception(
                        "More than one function "
                        + f"with {name} found."
                        + "Please specify force=True"
                    )
            elif len(result) == 1:
                func = result.pop()
                self._drop_single_function(
                    func.name,
                    func.inputs,
                    database=database,
                    aggregate=aggregate,
                )
                return
            else:
                raise Exception(f"No function found with name {name}")
        self._drop_single_function(
            name, input_types, database=database, aggregate=aggregate
        )

    def drop_uda(self, name, input_types=None, database=None, force=False):
        """Drop an aggregate function."""
        return self.drop_udf(
            name, input_types=input_types, database=database, force=force
        )

    def _drop_single_function(
        self, name, input_types, database=None, aggregate=False
    ):
        stmt = ddl.DropFunction(
            name,
            input_types,
            must_exist=False,
            aggregate=aggregate,
            database=database,
        )
        self.raw_sql(stmt)

    def _drop_all_functions(self, database):
        udfs = self.list_udfs(database=database)
        for fnct in udfs:
            stmt = ddl.DropFunction(
                fnct.name,
                fnct.inputs,
                must_exist=False,
                aggregate=False,
                database=database,
            )
            self.raw_sql(stmt)
        udafs = self.list_udas(database=database)
        for udaf in udafs:
            stmt = ddl.DropFunction(
                udaf.name,
                udaf.inputs,
                must_exist=False,
                aggregate=True,
                database=database,
            )
            self.raw_sql(stmt)

    def list_udfs(self, database=None, like=None):
        """Lists all UDFs associated with given database."""
        if not database:
            database = self.current_database
        statement = ddl.ListFunction(database, like=like, aggregate=False)
        cur = self.raw_sql(statement)
        result = self._get_udfs(cur, udf.ImpalaUDF)
        cur.release()
        return result

    def list_udas(self, database=None, like=None):
        """Lists all UDAFs associated with a given database."""
        if not database:
            database = self.current_database
        statement = ddl.ListFunction(database, like=like, aggregate=True)
        cur = self.raw_sql(statement)
        result = self._get_udfs(cur, udf.ImpalaUDA)
        cur.release()

        return result

    def _get_udfs(self, cur, klass):
        def _to_type(x):
            ibis_type = udf._impala_type_to_ibis(x.lower())
            return dt.dtype(ibis_type)

        tuples = cur.fetchall()
        if len(tuples) > 0:
            result = []
            for tup in tuples:
                out_type, sig = tup[:2]
                name, types = _split_signature(sig)
                types = _type_parser(types).types

                inputs = []
                for arg in types:
                    argm = _arg_type.match(arg)
                    var, simple = argm.groups()
                    if simple:
                        t = _to_type(simple)
                        inputs.append(t)
                    else:
                        t = _to_type(var)
                        inputs = rlz.listof(t)
                        # TODO
                        # inputs.append(varargs(t))
                        break

                output = udf._impala_type_to_ibis(out_type.lower())
                result.append(klass(inputs, output, name=name))
            return result
        else:
            return []

    def exists_udf(self, name: str, database: str | None = None) -> bool:
        """Checks if a given UDF exists within a specified database."""
        return bool(self.list_udfs(database=database, like=name))

    def exists_uda(self, name: str, database: str | None = None) -> bool:
        """Checks if a given UDAF exists within a specified database."""
        return bool(self.list_udas(database=database, like=name))

    def compute_stats(
        self,
        name: str,
        database: str | None = None,
        incremental: bool = False,
    ) -> None:
        """Issue a `COMPUTE STATS` command for a given table.

        Parameters
        ----------
        name
            Can be fully qualified (with database name)
        database
            Database name
        incremental
            If True, issue COMPUTE INCREMENTAL STATS
        """
        maybe_inc = 'INCREMENTAL ' if incremental else ''
        cmd = f'COMPUTE {maybe_inc}STATS'

        stmt = self._table_command(cmd, name, database=database)
        self.raw_sql(stmt)

    def invalidate_metadata(
        self,
        name: str | None = None,
        database: str | None = None,
    ) -> None:
        """Issue an `INVALIDATE METADATA` command.

        Optionally this applies to a specific table. See Impala documentation.

        Parameters
        ----------
        name
            Table name. Can be fully qualified (with database)
        database
            Database name
        """
        stmt = 'INVALIDATE METADATA'
        if name is not None:
            stmt = self._table_command(stmt, name, database=database)
        self.raw_sql(stmt)

    def refresh(self, name: str, database: str | None = None) -> None:
        """Reload HDFS block location metadata for a table.

        This can be useful after ingesting data as part of an ETL pipeline, for
        example.

        Related to `INVALIDATE METADATA`. See Impala documentation for more.

        Parameters
        ----------
        name
            Table name. Can be fully qualified (with database)
        database
            Database name
        """
        # TODO(wesm): can this statement be cancelled?
        stmt = self._table_command('REFRESH', name, database=database)
        self.raw_sql(stmt)

    def describe_formatted(
        self,
        name: str,
        database: str | None = None,
    ) -> pd.DataFrame:
        """Retrieve the results of a `DESCRIBE FORMATTED` command.

        See Impala documentation for more.

        Parameters
        ----------
        name
            Table name. Can be fully qualified (with database)
        database
            Database name
        """
        from ibis.backends.impala.metadata import parse_metadata

        stmt = self._table_command(
            'DESCRIBE FORMATTED', name, database=database
        )
        result = self._exec_statement(stmt)

        # Leave formatting to pandas
        for c in result.columns:
            result[c] = result[c].str.strip()

        return parse_metadata(result)

    def show_files(
        self,
        name: str,
        database: str | None = None,
    ) -> pd.DataFrame:
        """Retrieve results of a `SHOW FILES` command for a table.

        See Impala documentation for more.

        Parameters
        ----------
        name
            Table name. Can be fully qualified (with database)
        database
            Database name
        """
        stmt = self._table_command('SHOW FILES IN', name, database=database)
        return self._exec_statement(stmt)

    def list_partitions(self, name, database=None):
        stmt = self._table_command('SHOW PARTITIONS', name, database=database)
        return self._exec_statement(stmt)

    def table_stats(self, name, database=None):
        """Return results of `SHOW TABLE STATS` for the table `name`."""
        stmt = self._table_command('SHOW TABLE STATS', name, database=database)
        return self._exec_statement(stmt)

    def column_stats(self, name, database=None):
        """Return results of `SHOW COLUMN STATS` for the table `name`."""
        stmt = self._table_command(
            'SHOW COLUMN STATS', name, database=database
        )
        return self._exec_statement(stmt)

    def _exec_statement(self, stmt):
        return self.fetch_from_cursor(self.raw_sql(stmt), schema=None)

    def _table_command(self, cmd, name, database=None):
        qualified_name = self._fully_qualified_name(name, database)
        return f'{cmd} {qualified_name}'

    def _adapt_types(self, descr):
        names = []
        adapted_types = []
        for col in descr:
            names.append(col[0])
            impala_typename = col[1]
            typename = udf._impala_to_ibis_type[impala_typename.lower()]

            if typename == 'decimal':
                precision, scale = col[4:6]
                adapted_types.append(dt.Decimal(precision, scale))
            else:
                adapted_types.append(typename)
        return names, adapted_types

    def write_dataframe(
        self,
        df: pd.DataFrame,
        path: str,
        format: Literal['csv'] = 'csv',
    ) -> Any:
        """Write a pandas DataFrame to indicated file path.

        Parameters
        ----------
        df
            Pandas DataFrame
        path
            Absolute file path
        format
            File format
        """
        writer = DataFrameWriter(self, df)
        return writer.write_csv(path)
