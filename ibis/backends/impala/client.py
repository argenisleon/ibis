import time
import traceback

import pandas as pd
import sqlalchemy as sa

import ibis.common.exceptions as com
import ibis.expr.schema as sch
import ibis.expr.types as ir
import ibis.util as util
from ibis.backends.base import Database
from ibis.backends.base.sql.compiler import DDL, DML
from ibis.backends.base.sql.ddl import (
    AlterTable,
    InsertSelect,
    RenameTable,
    fully_qualified_re,
)
from ibis.backends.impala import ddl
from ibis.backends.impala.compat import HS2Error, impyla


class ImpalaDatabase(Database):
    def create_table(self, table_name, obj=None, **kwargs):
        """Dispatch to ImpalaClient.create_table.

        See that function's docstring for more
        """
        return self.client.create_table(
            table_name, obj=obj, database=self.name, **kwargs
        )

    def list_udfs(self, like=None):
        return self.client.list_udfs(like=like, database=self.name)

    def list_udas(self, like=None):
        return self.client.list_udas(like=like, database=self.name)


class ImpalaConnection:
    """Database connection wrapper."""

    def __init__(self, pool_size=8, database='default', **params):
        self.params = params
        self.database = database
        self.options = {}
        self.pool = sa.pool.QueuePool(
            self._new_cursor,
            pool_size=pool_size,
            # disable invoking rollback, because any transactions in impala are
            # automatic:
            # https://impala.apache.org/docs/build/html/topics/impala_transactions.html
            reset_on_return=False,
        )

        @sa.event.listens_for(self.pool, "checkout")
        def _(dbapi_connection, *_):
            """Update `dbapi_connection` options if they don't match `self`."""
            if (options := self.options) != dbapi_connection.options:
                dbapi_connection.options = options.copy()
                dbapi_connection.set_options()

    def set_options(self, options):
        self.options.update(options)

    def close(self):
        """Close all idle Impyla connections."""
        self.pool.dispose()

    def set_database(self, name):
        self.database = name

    def disable_codegen(self, disabled=True):
        self.options["DISABLE_CODEGEN"] = str(int(disabled))

    def execute(self, query):
        if isinstance(query, (DDL, DML)):
            query = query.compile()

        util.log(query)

        cursor = self.pool.connect()
        try:
            cursor.execute(query)
        except Exception:
            cursor.close()
            util.log(f'Exception caused by {query}: {traceback.format_exc()}')
            raise

        return cursor

    def fetchall(self, query):
        cur = self.execute(query)
        try:
            results = cur.fetchall()
        finally:
            cur.close()
        return results

    def _new_cursor(self):
        params = self.params.copy()
        con = impyla.connect(database=self.database, **params)

        # make sure the connection works
        cursor = con.cursor(user=params.get('user'), convert_types=True)
        cursor.ping()

        wrapper = ImpalaCursor(cursor, con, self.database, self.options.copy())
        wrapper.set_options()
        return wrapper

    @util.deprecated(instead="", version="4.0")
    def ping(self):  # pragma: no cover
        self.pool.connect()._cursor.ping()

    def release(self, cur):  # pragma: no cover
        pass


class ImpalaCursor:
    def __init__(self, cursor, impyla_con, database, options):
        self._cursor = cursor
        self.impyla_con = impyla_con
        self.database = database
        self.options = options

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    def close(self):
        try:
            self._cursor.close()
        except HS2Error as e:
            # connection was closed elsewhere
            already_closed_messages = [
                'invalid query handle',
                'invalid session',
            ]
            for message in already_closed_messages:
                if message in e.args[0].lower():
                    break
            else:
                raise

    def set_options(self):
        for k, v in self.options.items():
            query = f'SET {k} = {v!r}'
            self._cursor.execute(query)

    @property
    def description(self):
        return self._cursor.description

    def release(self):
        pass

    def execute(self, stmt):
        self._cursor.execute_async(stmt)
        self._wait_synchronous()

    def _wait_synchronous(self):
        # Wait to finish, but cancel if KeyboardInterrupt
        from impala.hiveserver2 import OperationalError

        loop_start = time.time()

        def _sleep_interval(start_time):
            elapsed = time.time() - start_time
            if elapsed < 0.05:
                return 0.01
            elif elapsed < 1.0:
                return 0.05
            elif elapsed < 10.0:
                return 0.1
            elif elapsed < 60.0:
                return 0.5
            return 1.0

        cur = self._cursor
        try:
            while True:
                state = cur.status()
                if self._cursor._op_state_is_error(state):
                    raise OperationalError("Operation is in ERROR_STATE")
                if not cur._op_state_is_executing(state):
                    break
                time.sleep(_sleep_interval(loop_start))
        except KeyboardInterrupt:
            util.log('Canceling query')
            self.cancel()
            raise

    def is_finished(self):
        return not self.is_executing()

    def is_executing(self):
        return self._cursor.is_executing()

    def cancel(self):
        self._cursor.cancel_operation()

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self, columnar=False):
        if columnar:
            return self._cursor.fetchcolumnar()
        else:
            return self._cursor.fetchall()


class ImpalaTable(ir.Table):
    """A physical table in the Impala-Hive metastore."""

    @property
    def _qualified_name(self):
        return self.op().args[0]

    @property
    def _unqualified_name(self):
        return self._match_name()[1]

    @property
    def _client(self):
        return self.op().source

    def _match_name(self):
        m = fully_qualified_re.match(self._qualified_name)
        if not m:
            raise com.IbisError(
                'Cannot determine database name from {}'.format(
                    self._qualified_name
                )
            )
        db, quoted, unquoted = m.groups()
        return db, quoted or unquoted

    @property
    def _database(self):
        return self._match_name()[0]

    def compute_stats(self, incremental=False):
        """Invoke Impala COMPUTE STATS command on the table."""
        return self._client.compute_stats(
            self._qualified_name, incremental=incremental
        )

    def invalidate_metadata(self):
        self._client.invalidate_metadata(self._qualified_name)

    def refresh(self):
        self._client.refresh(self._qualified_name)

    def metadata(self):
        """Return results of `DESCRIBE FORMATTED` statement."""
        return self._client.describe_formatted(self._qualified_name)

    describe_formatted = metadata

    def files(self):
        """Return results of SHOW FILES statement."""
        return self._client.show_files(self._qualified_name)

    def drop(self):
        """Drop the table from the database."""
        self._client.drop_table_or_view(self._qualified_name)

    def truncate(self):
        self._client.truncate_table(self._qualified_name)

    def insert(
        self,
        obj=None,
        overwrite=False,
        partition=None,
        values=None,
        validate=True,
    ):
        """Insert into an Impala table.

        Parameters
        ----------
        obj
            Table expression or DataFrame
        overwrite
            If True, will replace existing contents of table
        partition
            For partitioned tables, indicate the partition that's being
            inserted into, either with an ordered list of partition keys or a
            dict of partition field name to value. For example for the
            partition (year=2007, month=7), this can be either (2007, 7) or
            {'year': 2007, 'month': 7}.
        validate
            If True, do more rigorous validation that schema of table being
            inserted is compatible with the existing table

        Examples
        --------
        >>> t.insert(table_expr)  # doctest: +SKIP

        Completely overwrite contents
        >>> t.insert(table_expr, overwrite=True)  # doctest: +SKIP
        """
        if values is not None:
            raise NotImplementedError

        with self._client._setup_insert(obj) as expr:
            if validate:
                existing_schema = self.schema()
                insert_schema = expr.schema()
                if not insert_schema.equals(existing_schema):
                    _validate_compatible(insert_schema, existing_schema)

            if partition is not None:
                partition_schema = self.partition_schema()
                partition_schema_names = frozenset(partition_schema.names)
                expr = expr.projection(
                    [
                        column
                        for column in expr.columns
                        if column not in partition_schema_names
                    ]
                )
            else:
                partition_schema = None

            ast = self._client.compiler.to_ast(expr)
            select = ast.queries[0]
            statement = InsertSelect(
                self._qualified_name,
                select,
                partition=partition,
                partition_schema=partition_schema,
                overwrite=overwrite,
            )
            return self._client.raw_sql(statement.compile())

    def load_data(self, path, overwrite=False, partition=None):
        """Load data into an Impala table.

        Parameters
        ----------
        path
            Data to load
        overwrite
            Overwrite the existing data in the entire table or indicated
            partition
        partition
            If specified, the partition must already exist
        """
        if partition is not None:
            partition_schema = self.partition_schema()
        else:
            partition_schema = None

        stmt = ddl.LoadData(
            self._qualified_name,
            path,
            partition=partition,
            partition_schema=partition_schema,
        )

        return self._client.raw_sql(stmt.compile())

    @property
    def name(self):
        return self.op().name

    def rename(self, new_name, database=None):
        """Rename table inside Impala.

        References to the old table are no longer valid.
        """
        m = fully_qualified_re.match(new_name)
        if not m and database is None:
            database = self._database
        statement = RenameTable(
            self._qualified_name, new_name, new_database=database
        )
        self._client.raw_sql(statement)

        op = self.op().change_name(statement.new_qualified_name)
        return type(self)(op)

    @property
    def is_partitioned(self):
        """True if the table is partitioned."""
        return self.metadata().is_partitioned

    def partition_schema(self):
        """Return the schema for the partition columns."""
        schema = self.schema()
        name_to_type = dict(zip(schema.names, schema.types))

        result = self.partitions()

        partition_fields = []
        for x in result.columns:
            if x not in name_to_type:
                break
            partition_fields.append((x, name_to_type[x]))

        pnames, ptypes = zip(*partition_fields)
        return sch.Schema(pnames, ptypes)

    def add_partition(self, spec, location=None):
        """Add a new table partition.

        This API creates any necessary new directories in HDFS.

        Partition parameters can be set in a single DDL statement or you can
        use `alter_partition` to set them after the fact.
        """
        part_schema = self.partition_schema()
        stmt = ddl.AddPartition(
            self._qualified_name, spec, part_schema, location=location
        )
        return self._client.raw_sql(stmt)

    def alter(
        self,
        location=None,
        format=None,
        tbl_properties=None,
        serde_properties=None,
    ):
        """Change settings and parameters of the table.

        Parameters
        ----------
        location
            For partitioned tables, you may want the alter_partition function
        format
            Table format
        tbl_properties
            Table properties
        serde_properties
            Serialization/deserialization properties
        """

        def _run_ddl(**kwds):
            stmt = AlterTable(self._qualified_name, **kwds)
            return self._client.raw_sql(stmt)

        return self._alter_table_helper(
            _run_ddl,
            location=location,
            format=format,
            tbl_properties=tbl_properties,
            serde_properties=serde_properties,
        )

    def set_external(self, is_external=True):
        """Toggle the `EXTERNAL` table property."""
        self.alter(tbl_properties={'EXTERNAL': is_external})

    def alter_partition(
        self,
        spec,
        location=None,
        format=None,
        tbl_properties=None,
        serde_properties=None,
    ):
        """Change settings and parameters of an existing partition.

        Parameters
        ----------
        spec
            The partition keys for the partition being modified
        location
            Location of the partition
        format
            Table format
        tbl_properties
            Table properties
        serde_properties
            Serialization/deserialization properties
        """
        part_schema = self.partition_schema()

        def _run_ddl(**kwds):
            stmt = ddl.AlterPartition(
                self._qualified_name, spec, part_schema, **kwds
            )
            return self._client.raw_sql(stmt)

        return self._alter_table_helper(
            _run_ddl,
            location=location,
            format=format,
            tbl_properties=tbl_properties,
            serde_properties=serde_properties,
        )

    def _alter_table_helper(self, f, **alterations):
        results = []
        for k, v in alterations.items():
            if v is None:
                continue
            result = f(**{k: v})
            results.append(result)
        return results

    def drop_partition(self, spec):
        """Drop an existing table partition."""
        part_schema = self.partition_schema()
        stmt = ddl.DropPartition(self._qualified_name, spec, part_schema)
        return self._client.raw_sql(stmt)

    def partitions(self):
        """Return information about the table's partitions.

        Raises an exception if the table is not partitioned.
        """
        return self._client.list_partitions(self._qualified_name)

    def stats(self) -> pd.DataFrame:
        """Return results of `SHOW TABLE STATS`.

        If not partitioned, contains only one row.

        Returns
        -------
        DataFrame
            Table statistics
        """
        return self._client.table_stats(self._qualified_name)

    def column_stats(self) -> pd.DataFrame:
        """Return results of `SHOW COLUMN STATS`.

        Returns
        -------
        DataFrame
            Column statistics
        """
        return self._client.column_stats(self._qualified_name)


# ----------------------------------------------------------------------
# ORM-ish usability layer


class ScalarFunction:
    def drop(self):
        pass


class AggregateFunction:
    def drop(self):
        pass


def _validate_compatible(from_schema, to_schema):
    if set(from_schema.names) != set(to_schema.names):
        raise com.IbisInputError('Schemas have different names')

    for name in from_schema:
        lt = from_schema[name]
        rt = to_schema[name]
        if not lt.castable(rt):
            raise com.IbisInputError(f'Cannot safely cast {lt!r} to {rt!r}')
