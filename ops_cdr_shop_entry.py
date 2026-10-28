from pathlib import Path

import cdr_shop_patch
import did_module
import invoice_module
import monitoring_module
import sip_buffer_module
import system_monitoring_module
import ops_entry


app = ops_entry.app
cdr_shop_patch.install(app, ops_entry.main, ops_entry.db)
# Register the standalone SIP buffer before the legacy DID module.  FastAPI
# resolves identical paths in registration order, so /sip-crm and the edge
# config endpoint below are served from the isolated sip_buffer.db while the
# existing DID billing remains unchanged.
sip_buffer_module.install(app, ops_entry.main, ops_entry.db, Path(__file__).parent)
did_module.install(app, ops_entry.main, ops_entry.db, Path(__file__).parent)
monitoring_module.install(app, ops_entry.main, ops_entry.db, Path(__file__).parent)
invoice_module.install(app, ops_entry.main, ops_entry.db, Path(__file__).parent)
system_monitoring_module.install(app, ops_entry.main, ops_entry.db, Path(__file__).parent)
