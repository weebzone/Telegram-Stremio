import asyncio
from typing import Optional
from Backend.helper.subscription_checker import subscription_checker_loop
from Backend.helper.settings_manager import SettingsManager

_task: Optional[asyncio.Task] = None


def is_running() -> bool:
    return _task is not None and not _task.done()

async def start(bot) -> bool:
    global _task
    if is_running():
        return False
    _task = asyncio.create_task(subscription_checker_loop(bot))
    LOGGER.info("Subscription Checker Task Started.")
    return True


async def stop() -> bool:
    global _task
    if not is_running():
        _task = None
        return False

    _task.cancel()
    try:
        await _task
    except asyncio.CancelledError:
        pass
    except Exception as e:
        LOGGER.warning(f"Error while stopping subscription checker task: {e}")
    finally:
        _task = None

    LOGGER.info("Subscription Checker Task Stopped.")
    return True


async def sync(bot) -> None:
    if SettingsManager.current().subscription:
        await start(bot)
