import os
import sys
import subprocess
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from Backend.fastapi.security.credentials import get_current_user
from Backend.helper.metadata_manager import metadata_manager

router = APIRouter()

@router.post("/update")
async def update_server(current_user: str = Depends(get_current_user)):
    """
    Pull latest changes from git without restarting.
    """
    try:
        # Get current commit before pulling
        old_commit_process = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True
        )
        old_commit = old_commit_process.stdout.strip() if old_commit_process.returncode == 0 else "unknown"
        
        # Get current branch name
        branch_process = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"], 
            capture_output=True, 
            text=True
        )
        
        if branch_process.returncode != 0:
            raise HTTPException(status_code=500, detail="Failed to detect current branch")
        
        current_branch = branch_process.stdout.strip()
        
        # Run git pull with explicit remote and branch
        process = subprocess.run(
            ["git", "pull", "origin", current_branch], 
            capture_output=True, 
            text=True
        )
        
        if process.returncode != 0:
            raise HTTPException(status_code=500, detail=f"Git pull failed: {process.stderr}")
        
        # Get new commit after pulling
        new_commit_process = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True
        )
        new_commit = new_commit_process.stdout.strip() if new_commit_process.returncode == 0 else "unknown"
        
        # Get commit message
        commit_msg_process = subprocess.run(
            ["git", "log", "-1", "--pretty=%B"],
            capture_output=True,
            text=True
        )
        commit_message = commit_msg_process.stdout.strip() if commit_msg_process.returncode == 0 else ""
        
        # Check if there were any updates
        if old_commit == new_commit:
            update_status = "Already up to date"
        else:
            update_status = f"Updated from {old_commit} to {new_commit}"
        
        return {
            "message": update_status,
            "old_commit": old_commit,
            "new_commit": new_commit,
            "commit_message": commit_message,
            "git_output": process.stdout,
            "note": "Updates pulled successfully. Use Restart Server if you want to apply changes."
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/restart")
async def restart_server(background_tasks: BackgroundTasks, current_user: str = Depends(get_current_user)):
    """
    Restart the server process.
    """
    background_tasks.add_task(restart_process)
    return {"message": "Server restarting..."}

@router.post("/fixmetadata")
async def trigger_fix_metadata(background_tasks: BackgroundTasks, current_user: str = Depends(get_current_user)):
    """
    Trigger the fix metadata background task.
    """
    if metadata_manager.IS_RUNNING:
         raise HTTPException(status_code=409, detail="Metadata fix is already running")

    # Start the task in background
    background_tasks.add_task(metadata_manager.run_fix_metadata)
    return {"message": "Metadata fix started in background"}

def restart_process():
    """
    Restart the current python process.
    This is a helper function that might need adjustment based on how the app is run (Docker vs bare metal).
    For Docker, exiting usually triggers a container restart policy if set.
    """
    import time
    import shutil
    
    time.sleep(1) # Give time for response to be sent
    
    uv_path = shutil.which("uv")
    if uv_path:
        # Use os.execl which replaces the process
        os.execl(uv_path, uv_path, "run", "-m", "Backend")
    else:
        # Fallback to python
        os.execv(sys.executable, [sys.executable, '-m', 'Backend'])
