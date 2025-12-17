import uvicorn
import logging
import copy
from Backend.config import Telegram
from Backend.fastapi.main import app

Port = Telegram.PORT

# Configure Logging to suppress ProtocolError
log_config = copy.deepcopy(uvicorn.config.LOGGING_CONFIG)

# Ensure 'filters' key exists
if "filters" not in log_config:
    log_config["filters"] = {}

# 1. Add Filter Definition
log_config["filters"]["protocol_filter"] = {
    "()": "Backend.fastapi.filters.ProtocolErrorFilter"
}

# 2. Add Filter to Handlers
# 'default' handler handles 'uvicorn.error'
if "default" in log_config["handlers"]:
    if "filters" not in log_config["handlers"]["default"]:
        log_config["handlers"]["default"]["filters"] = []
    log_config["handlers"]["default"]["filters"].append("protocol_filter")

# 'access' handler handles 'uvicorn.access' (Requests)
if "access" in log_config["handlers"]:
    if "filters" not in log_config["handlers"]["access"]:
        log_config["handlers"]["access"]["filters"] = []
    log_config["handlers"]["access"]["filters"].append("protocol_filter")

config = uvicorn.Config(app=app, host='0.0.0.0', port=Port, log_config=log_config)
server = uvicorn.Server(config)
