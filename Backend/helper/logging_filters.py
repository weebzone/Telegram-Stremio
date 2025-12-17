import logging

class ProtocolErrorFilter(logging.Filter):
    def filter(self, record):
        if record.exc_info:
            _, exc_value, _ = record.exc_info
            if "LocalProtocolError" in str(type(exc_value)) or "Too little data" in str(exc_value):
                return False
        return True
