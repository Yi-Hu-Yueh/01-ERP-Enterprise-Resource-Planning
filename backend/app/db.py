from contextlib import contextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, event
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session

from .models import Actor
from .stock_ledger import register_exact_sql

ROOT = Path(__file__).resolve().parents[2]
DEMO_ACTOR_ID = "00000000-0000-4000-8000-000000000001"
DEMO_LABEL = "LOCAL DEMO MODE — NO VERIFIED LOGIN"


def make_engine(path):
    engine = create_engine(URL.create("sqlite+pysqlite", database=str(Path(path).resolve())),
                           connect_args={"check_same_thread": False}, pool_pre_ping=True)

    @event.listens_for(engine, "connect")
    def configure(connection, _):
        connection.isolation_level = None
        register_exact_sql(connection)
        from .completion_authorization import register_denied_sql
        register_denied_sql(connection)
        from .finished_goods_authorization import register_denied_sql as deny_finished_goods
        deny_finished_goods(connection)
        from .sales_shipment_authorization import register_denied_sql as deny_sales_shipment
        deny_sales_shipment(connection)
        from .customer_return_authorization import register_denied_sql as deny_customer_return
        deny_customer_return(connection)
        from .stocktake_authorization import register_denied_sql as deny_stocktake, register_edit_sql
        deny_stocktake(connection)
        register_edit_sql(connection)
        from .adjustment_authorization import register_denied_sql as deny_adjustment, register_edit_sql as adjustment_edit_sql
        deny_adjustment(connection)
        adjustment_edit_sql(connection)
        from .transfer_authorization import register_denied_sql as deny_transfer, register_edit_sql as transfer_edit_sql
        deny_transfer(connection)
        transfer_edit_sql(connection)
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=3000")
        connection.execute("PRAGMA synchronous=FULL")

    @event.listens_for(engine, "begin")
    def begin(connection):
        connection.exec_driver_sql("BEGIN IMMEDIATE" if connection.get_execution_options().get("erp_write") else "BEGIN")

    return engine


def migrate(path):
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = make_engine(path)
    try:
        with engine.connect() as connection:
            # Outside an SQLAlchemy transaction, before migrations or other writers.
            connection.connection.driver_connection.execute("PRAGMA journal_mode=WAL")
        config = Config(str(ROOT / "backend" / "alembic.ini"))
        config.set_main_option("script_location", str(ROOT / "backend" / "migrations"))
        with engine.connect().execution_options(erp_write=True) as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
    finally:
        engine.dispose()


class Database:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.engine = make_engine(self.path)
        from .warehouse_freeze import install_freeze_checks
        install_freeze_checks()

    @contextmanager
    def write(self):
        with Session(self.engine, expire_on_commit=False) as session:
            try:
                session.connection(execution_options={"erp_write": True})
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise

    @contextmanager
    def read(self):
        with Session(self.engine) as session:
            yield session

    def seed_operator(self):
        with self.write() as session:
            actor = session.get(Actor, DEMO_ACTOR_ID)
            if actor is None:
                session.add(Actor(id=DEMO_ACTOR_ID, login_key="LOCAL_DEMO", display_name=DEMO_LABEL,
                                  external_subject=None))
            elif actor.login_key != "LOCAL_DEMO" or actor.display_name != DEMO_LABEL:
                raise RuntimeError("The stable demo operator identity conflicts with existing data; no records changed.")

