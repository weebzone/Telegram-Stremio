"""
api_routes.py — compatibility shim.

Re-exports the split admin API handlers from Backend.fastapi.routes.api so
existing imports in main.py keep working unchanged.
"""

from Backend.fastapi.routes.api import *  # noqa: F401,F403
from Backend.fastapi.routes.api import __all__  # noqa: F401
