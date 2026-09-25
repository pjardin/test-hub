from django.apps import AppConfig
from django.db.backends.signals import connection_created


def _sqlite_tuning(sender, connection, **kwargs):
    """WAL + busy timeout: web threads and runner threads share one SQLite
    file, and without this the second writer gets 'database is locked'."""
    if connection.vendor != "sqlite":
        return
    cursor = connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL;")
    cursor.execute("PRAGMA synchronous=NORMAL;")
    cursor.execute("PRAGMA busy_timeout=20000;")
    cursor.close()


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "core"
    verbose_name = "Test Hub"

    def ready(self):
        connection_created.connect(_sqlite_tuning)
