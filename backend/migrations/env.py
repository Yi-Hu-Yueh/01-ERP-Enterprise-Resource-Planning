from alembic import context
from backend.app.models import Base
from backend.app import purchase_models  # Register purchase entities for CLI migration metadata too.
from backend.app import receiving_models
from backend.app import return_models
from backend.app import bom_models
from backend.app import routing_models
from backend.app import work_order_models
from backend.app import issue_models
from backend.app import completion_models
from backend.app import finished_goods_models
from backend.app import sales_models
from backend.app import sales_shipment_models
from backend.app import customer_return_models
from backend.app import stocktake_models
from backend.app import adjustment_models
from backend.app import transfer_models

connection = context.config.attributes.get("connection")
if connection is None:
    raise RuntimeError("Use python scripts/migrate.py with a project-local database path.")
context.configure(connection=connection, target_metadata=Base.metadata, render_as_batch=True,
                  transactional_ddl=True)
with context.begin_transaction():
    context.run_migrations()

