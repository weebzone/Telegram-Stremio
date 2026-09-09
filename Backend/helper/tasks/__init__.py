"""
tasks/ — background jobs and message-task helpers.

  - task_manager   edit/delete Telegram messages (FloodWait-safe)
  - subscription   subscription expiry checker loop
  - pinger         keep-alive HTTP ping
  - backup         config export/import
"""

from Backend.helper.tasks.task_manager import (
    delete_message,
    delete_messages_batch,
    edit_message,
)

# Delayed imports to break circular dependency with Backend.db
# (Backend/__init__.py imports Database which pulls task_manager via database.py,
#  and subscription/backup import Backend.db at module level.)
from Backend.helper.tasks.pinger import ping

def __getattr__(name):
    if name == "subscription_task_manager":
        from Backend.helper.tasks import subscription as _mod
        return _mod
    if name == "export_config":
        from Backend.helper.tasks.backup import export_config
        return export_config
    if name == "import_config":
        from Backend.helper.tasks.backup import import_config
        return import_config
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = [
    "delete_message",
    "delete_messages_batch",
    "edit_message",
    "subscription_task_manager",
    "ping",
    "export_config",
    "import_config",
]
