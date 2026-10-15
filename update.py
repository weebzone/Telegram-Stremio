from logging import FileHandler, StreamHandler, INFO, Formatter, basicConfig, error as log_error, info as log_info
from os import path as ospath, environ
from pathlib import Path
from subprocess import run as srun
from dotenv import load_dotenv
from datetime import datetime
import pytz
import shutil

IST = pytz.timezone("Asia/Kolkata")

class ISTFormatter(Formatter):
    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, IST)
        return dt.strftime(datefmt or "%d-%b-%y %I:%M:%S %p")

log_file = "log.txt"
if ospath.exists(log_file):
    with open(log_file, "w") as f:
        f.truncate(0)
if Path(".git").exists():
    shutil.rmtree(".git")

file_handler = FileHandler(log_file)
stream_handler = StreamHandler()
formatter = ISTFormatter("[%(asctime)s] [%(levelname)s] - %(message)s", "%d-%b-%y %I:%M:%S %p")
file_handler.setFormatter(formatter)
stream_handler.setFormatter(formatter)
basicConfig(handlers=[file_handler, stream_handler], level=INFO)

# ── Load config.env as the base (for DATABASE URI, etc.) ─────────────────────
load_dotenv("config.env")


def _fetch_upstream_from_db() -> tuple[str | None, str]:
    try:
        from pymongo import MongoClient
        raw_uris = environ.get("DATABASE", "")          
        uris = [u.strip() for u in raw_uris.replace(",", " ").split() if u.strip()]
        if not uris:
            log_info("update.py: No DATABASE found — skipping DB settings lookup.")
            return None, "master"

        tracking_uri = uris[0]
        client = MongoClient(tracking_uri, serverSelectionTimeoutMS=5000)
        doc = client["dbFyvio"]["settings"].find_one({"_id": "app_settings"})
        client.close()

        if doc:
            repo   = (doc.get("upstream_repo")   or "").strip() or None
            branch = (doc.get("upstream_branch") or "").strip() or "master"
            return repo, branch

    except Exception as exc:
        log_error(f"update.py: DB lookup failed ({exc}) — falling back to config.env.")

    return None, "master"


# ── Priority: DB value  >  config.env value ──────────────────────────────────
db_repo, db_branch = _fetch_upstream_from_db()

UPSTREAM_REPO   = db_repo   or environ.get("UPSTREAM_REPO",   "").strip() or None
UPSTREAM_BRANCH = db_branch or environ.get("UPSTREAM_BRANCH", "").strip() or "dev"

# ── Git update ────────────────────────────────────────────────────────────────
if UPSTREAM_REPO:
    if Path(".git").exists():
        srun(["rm", "-rf", ".git"])

    update_cmd = (
        f"git init -q && "
        f"git config --global user.email 'doc.adhikari@gmail.com' && "
        f"git config --global user.name 'weebzone' && "
        f"git add . && git commit -sm 'update' -q && "
        f"git remote add origin {UPSTREAM_REPO} && "
        f"git fetch origin -q && "
        f"git reset --hard origin/{UPSTREAM_BRANCH} -q"
    )

    update = srun(update_cmd, shell=True)
    repo = UPSTREAM_REPO.strip("/").split("/")
    repo_url = f"https://github.com/{repo[-2]}/{repo[-1]}"
    log_info(f"UPSTREAM_REPO: {repo_url} | UPSTREAM_BRANCH: {UPSTREAM_BRANCH}")

    if update.returncode == 0:
        log_info("Successfully updated with latest commits!!")
        commit_check = srun(["git", "rev-parse", "HEAD"], capture_output=True, text=True)
        if commit_check.returncode == 0:
            log_info(f"Latest commit ID: {commit_check.stdout.strip()}")
    else:
        log_error("❌ Update failed! Retry or ask for support.")
