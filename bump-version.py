#!/usr/bin/env python3
import re
import sys
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ImportError:  # pragma: no cover - fallback for older interpreters
    tomllib = None

VERSION_RE = r"[0-9]+\.[0-9]+\.[0-9]+"


def bump_version(version: str, part: str) -> str:
    major, minor, patch = map(int, version.split("."))
    if part == "patch":
        patch += 1
    elif part == "minor":
        minor += 1
        patch = 0
    elif part == "major":
        major += 1
        minor = patch = 0
    else:
        raise ValueError(f"Unknown part to bump: {part}")
    return f"{major}.{minor}.{patch}"


def get_project_name(pyproject_path: Path) -> str:
    content = pyproject_path.read_text()
    if tomllib is not None:
        try:
            data = tomllib.loads(content)
            name = data.get("project", {}).get("name")
            if name:
                return name
        except Exception:
            pass  # fall through to regex fallback
    match = re.search(r'(?m)^\[project\]\s*\n(?:.*\n)*?name\s*=\s*"([^"]+)"', content)
    if not match:
        print("Error: could not determine project name from pyproject.toml")
        sys.exit(1)
    return match.group(1)


def update_pyproject(path: Path, new_version: str):
    content = path.read_text()
    updated_content, count = re.subn(
        rf'version\s*=\s*"{VERSION_RE}"',
        f'version = "{new_version}"',
        content,
        count=1,
    )
    if count == 0:
        print("Error: version field not found in pyproject.toml")
        sys.exit(1)
    path.write_text(updated_content)
    print(f"Updated pyproject.toml to version {new_version}")


def update_init(path: Path, new_version: str):
    content = path.read_text()
    updated_content, count = re.subn(
        rf'__version__\s*=\s*"{VERSION_RE}"',
        f'__version__ = "{new_version}"',
        content,
        count=1,
    )
    if count == 0:
        print("Error: __version__ not found in Backend/__init__.py")
        sys.exit(1)
    path.write_text(updated_content)
    print(f"Updated Backend/__init__.py to version {new_version}")


def normalize_package_name(name: str) -> str:
    """PEP 503 normalization: lowercase, runs of -_. collapse to a single '-'.

    uv (like pip/PyPI) normalizes package names before writing uv.lock, so
    a pyproject.toml name of "Telegram-Stremio" or "telegram_stremio" ends
    up as "telegram-stremio" in the lockfile. We must normalize the same
    way before searching, or the lookup silently misses.
    """
    return re.sub(r"[-_.]+", "-", name).lower()


def update_uv_lock(path: Path, project_name: str, new_version: str):
    """Update the version of the project's own entry inside uv.lock.

    uv.lock lists every dependency's version too, so a naive "replace the
    first version string" is unsafe. We only touch the [[package]] block
    whose name matches the project and whose source is the local package
    (source = { virtual = "." }), wherever that block currently sits in
    the (alphabetically sorted) file.
    """
    if not path.exists():
        print("Warning: uv.lock not found, skipping.")
        return

    content = path.read_text()
    normalized_name = normalize_package_name(project_name)
    pattern = re.compile(
        r'(name = "' + re.escape(normalized_name) + r'"\n'
        r'version = ")' + VERSION_RE + r'("\n'
        r'source = \{ virtual = "\." \})'
    )
    updated_content, count = pattern.subn(
        rf'\g<1>{new_version}\g<2>', content
    )
    if count == 0:
        print(
            f"Warning: could not find local package entry for "
            f"\"{project_name}\" (normalized: \"{normalized_name}\") "
            f"in uv.lock — skipped. Run `uv lock` afterwards to sync it manually."
        )
        return
    if count > 1:
        print(
            f"Warning: found {count} matching entries in uv.lock, "
            f"expected exactly 1 — please check the file manually."
        )
    path.write_text(updated_content)
    print(f"Updated uv.lock to version {new_version}")


def main(part: str = "patch"):
    pyproject_path = Path("pyproject.toml")
    init_path = Path("Backend/__init__.py")
    uv_lock_path = Path("uv.lock")

    if not pyproject_path.exists() or not init_path.exists():
        print("Error: pyproject.toml or Backend/__init__.py not found.")
        sys.exit(1)

    project_name = get_project_name(pyproject_path)

    # Read current version from pyproject.toml
    content = pyproject_path.read_text()
    match = re.search(rf'version\s*=\s*"({VERSION_RE})"', content)
    if not match:
        print("Error: version not found in pyproject.toml")
        sys.exit(1)

    current_version = match.group(1)
    new_version = bump_version(current_version, part)

    update_pyproject(pyproject_path, new_version)
    update_init(init_path, new_version)
    update_uv_lock(uv_lock_path, project_name, new_version)

    print(f"\nBumped {current_version} -> {new_version} ({part})")


if __name__ == "__main__":
    part = sys.argv[1] if len(sys.argv) > 1 else "patch"  # default bump is patch
    main(part)
