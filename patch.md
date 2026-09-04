# patch.md — Custom Patch (donation banner removal)

Purpose: this repo's server tracks the `fix` branch, which = latest upstream code + this patch.
When upstream updates and you want the patch re-applied, sync the fork, then ask the agent:
**"apply patch"** — it must read this file, check the current code, and re-apply accordingly.

## What this patch does

1. Removes the obfuscated donation banner injected into Stremio stream results.
2. Ensures `Caddyfile` stays out of git (contains server-specific config).

## Files & steps

### 1. `Backend/fastapi/themes.py`

Remove the entire `__x7()` function at the end of the file:

```python
def __x7():
    import base64 as __b
    __u = __b.b64decode("aHR0cHM6Ly9kb25hdGUud2VlYnpvbmV4LndvcmtlcnMuZGV2").decode()
    __n = __b.b64decode("4q2QIERvbmF0aW9uIG5lZWRlZC4=").decode()
    __t = __b.b64decode("Q2xpY2sgaGVyZSB0byBkb25hdGUgdG8ga2VlcCB0aGUgcHJvamVjdCBhbGl2ZS4=").decode()
    return {"name": __n, "title": __t, "externalUrl": __u}
```

> If upstream renamed/moved this function, search for the markers below and remove whatever matches.

### 2. `Backend/fastapi/routes/stremio_routes.py`

- Remove `__x7` from the themes import:
  ```python
  from Backend.fastapi.themes import DEFAULT_THEME, DEFAULT_STYLE, get_theme, __x7
  # becomes
  from Backend.fastapi.themes import DEFAULT_THEME, DEFAULT_STYLE, get_theme
  ```
- Empty-stream fallback (replace `[__x7()]` with `[]`):
  ```python
  if not streams:
      return {"streams": [__x7()]}   # → {"streams": []}
  ```
- Remove the banner prepend line entirely:
  ```python
  streams.insert(0, __x7())          # delete this line
  ```

### 3. `.gitignore`

Ensure it contains a line with exactly: `Caddyfile`

## Markers to locate the code in ANY version

- grep for: `__x7` → all code references must disappear after applying.
- grep for: `weebzonex` or `donate` in `Backend/**/*.py` → no matches should remain.
- Banner base64 signature: `aHR0cHM6Ly9kb25hdGUu` (URL of the donation page).

## Verification checklist (after applying)

1. `__x7` / `weebzonex` / `donate` produce **no matches** in `Backend/**/*.py` (only doc references in `patch.md`, `.github/skills/...`, `.github/copilot-instructions.md` are OK).
2. Syntax check passes:
   ```bash
   uv run python -m py_compile Backend/fastapi/themes.py Backend/fastapi/routes/stremio_routes.py
   ```
3. Empty-stream fallback returns `{"streams": []}`.
4. `.gitignore` contains `Caddyfile`.

## Release workflow

1. Sync fork master with upstream (GitHub "Sync fork" button).
2. Ask the agent to apply this patch against the latest code.
3. Agent commits to `fix` and pushes (`origin/fix`).
4. Server: Settings → Restart App (pulls `origin/fix` with the patch applied).
