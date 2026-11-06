import time

MAX_FAILURES = 5
WINDOW = 600  # seconds

_failures: dict[str, list[float]] = {}  # shortcut: per-process memory, resets on restart; fine for one admin login


def _now() -> float:
    return time.time()


def _recent(ip: str) -> list[float]:
    cutoff = _now() - WINDOW
    kept = [t for t in _failures.get(ip, []) if t > cutoff]
    if kept:
        _failures[ip] = kept
    else:
        _failures.pop(ip, None)
    return kept


def is_blocked(ip: str) -> bool:
    return len(_recent(ip)) >= MAX_FAILURES


def record_failure(ip: str) -> None:
    _recent(ip)
    _failures.setdefault(ip, []).append(_now())


def record_success(ip: str) -> None:
    _failures.pop(ip, None)


def reset() -> None:
    _failures.clear()
