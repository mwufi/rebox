"""Database access as role ``rebox_app`` (RLS applies).

Every transaction must start with ``rebox_set_view(visible, writable)`` (CONTRACTS §2), so
all access goes through ``Db.view()``::

    with db.view() as conn:                 # visible = all public layers, writable = none
        conn.execute("select ...")

The worker never writes graph tables directly (that is the claims writer's job); it reads
claims/entities and writes the operational tables (job, connector_state, raw_record,
resolution_log, review_item), which have no RLS.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

PUBLIC_LAYERS_SQL = "select id from layer where scope = 'public' order by id"


def public_layers(conn: psycopg.Connection) -> list[str]:
    return [r["id"] if isinstance(r, dict) else r[0] for r in conn.execute(PUBLIC_LAYERS_SQL)]


def set_view(conn: psycopg.Connection, visible: Sequence[str], writable: Sequence[str] = ()) -> None:
    conn.execute("select rebox_set_view(%s::text[], %s::text[])", (list(visible), list(writable)))


class Db:
    def __init__(self, url: str, *, min_size: int = 1, max_size: int = 4) -> None:
        self.pool = ConnectionPool(
            url,
            min_size=min_size,
            max_size=max_size,
            kwargs={"row_factory": dict_row, "autocommit": False},
            open=True,
            name="rebox-worker",
        )

    @contextmanager
    def view(
        self, visible: Sequence[str] | None = None, writable: Sequence[str] = ()
    ) -> Iterator[psycopg.Connection]:
        """One transaction with the view set. ``visible=None`` means all public layers.

        Commits on normal exit, rolls back on exception.
        """
        with self.pool.connection() as conn, conn.transaction():
            layers = public_layers(conn) if visible is None else visible
            set_view(conn, layers, writable)
            yield conn

    def close(self) -> None:
        self.pool.close()

    def __enter__(self) -> Db:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
