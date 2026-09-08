import telegram_cdr_shop_patch
import telegram_cdr_check
import telegram_portal_entry


app = telegram_portal_entry.app
telegram_cdr_shop_patch.install(app, telegram_portal_entry.bot)
telegram_cdr_check.install(telegram_portal_entry.bot)
