import os

# Backend/__init__.py builds Database() at import time and needs 2 URIs. Motor connects lazily, so fake ones are safe.
os.environ.setdefault("DATABASE", "mongodb://localhost/a,mongodb://localhost/b")
