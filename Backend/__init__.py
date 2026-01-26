from Backend.helper.database import Database
from time import time
from datetime import datetime
import pytz

timezone = pytz.timezone("Asia/Kolkata")
now = datetime.now(timezone)
StartTime = time()


USE_DEFAULT_ID: str = None
db = Database()  

__version__ = "2.0.0"
