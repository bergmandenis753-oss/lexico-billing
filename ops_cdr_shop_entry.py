import cdr_shop_patch
import ops_entry


app = ops_entry.app
cdr_shop_patch.install(app, ops_entry.main, ops_entry.db)
