from pathlib import Path

import cdr_shop_patch
import did_module
import ops_entry


app = ops_entry.app
cdr_shop_patch.install(app, ops_entry.main, ops_entry.db)
did_module.install(app, ops_entry.main, ops_entry.db, Path(__file__).parent)
