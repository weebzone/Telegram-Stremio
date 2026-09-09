from datetime import datetime
from time import time

import pytz

#----- Shared application state (defined early to avoid circular imports)
timezone = pytz.timezone("Asia/Kolkata")
now = datetime.now(timezone)
StartTime = time()

USE_DEFAULT_ID: str = None
MANUAL_SESSION: dict = None

__version__ = "5.0.5"

from Backend.helper.database import Database

db = Database()
