from datetime import datetime
from time import time

import pytz

# Constants first — must exist before any helper imports Backend
__version__ = "5.0.5"
timezone = pytz.timezone("Asia/Kolkata")
now = datetime.now(timezone)
StartTime = time()
USE_DEFAULT_ID: str = None
MANUAL_SESSION: dict = None

from Backend.helper.database import Database

db = Database()
