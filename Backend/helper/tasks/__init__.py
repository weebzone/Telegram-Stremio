"""
tasks/ — background jobs and message-task helpers.

  - task_manager   edit/delete Telegram messages (FloodWait-safe)
  - subscription   subscription expiry checker loop
  - pinger         keep-alive HTTP ping
  - backup         config export/import

All exports are lazy so importing task_manager during Backend bootstrap
(database → delete_message) does not pull backup/subscription which import
Backend.db / __version__.
"""

__all__ = [
    "delete_message",
    "delete_messages_batch",
    "edit_message",
    "subscription_task_manager",
    "ping",
    "export_config",
    "import_config",
]

_LAZY = {
    "delete_message": ("Backend.helper.tasks.task_manager", "delete_message"),
    "delete_messages_batch": ("Backend.helper.tasks.task_manager", "delete_messages_batch"),
    "edit_message": ("Backend.helper.tasks.task_manager", "edit_message"),
    "subscription_task_manager": ("Backend.helper.tasks.subscription", None),  # whole module
    "ping": ("Backend.helper.tasks.pinger", "ping"),
    "export_config": ("Backend.helper.tasks.backup", "export_config"),
    "import_config": ("Backend.helper.tasks.backup", "import_config"),
}


def __getattr__(name: str):
    if name not in _LAZY:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib
    mod_name, attr = _LAZY[name]
    mod = importlib.import_module(mod_name)
    value = mod if attr is None else getattr(mod, attr)
    globals()[name] = value
    return value
