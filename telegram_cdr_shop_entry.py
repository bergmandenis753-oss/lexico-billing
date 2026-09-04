import telegram_cdr_shop_patch
import telegram_portal_entry


app = telegram_portal_entry.app
telegram_cdr_shop_patch.install(app, telegram_portal_entry.bot)
