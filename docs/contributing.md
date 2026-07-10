# Contributing

Contributions are welcome — whether it's code, docs, bug reports, or ideas.

## Ways to help

- 🐛 **Report bugs** with clear steps to reproduce.
- 💡 **Request features** with a short description of the problem you're solving.
- 📝 **Improve docs** (including this site — the pages live under `docs/`).
- 🔧 **Send pull requests** for fixes and features.

## Setting up for development

```bash
git clone https://github.com/weebzone/Telegram-Stremio
cd Telegram-Stremio
cp sample_config.env config.env   # fill in your values
```

The project is a **FastAPI** app with a **PyroFork** Telegram bot and **MongoDB** storage.
Dependencies are listed in `requirements.txt` (installed with `uv` in the Docker image).

## Building these docs locally

```bash
cd docs
pip install -r requirements.txt
sphinx-build -b html . _build/html
```

Then open `docs/_build/html/index.html` in your browser.

## Reporting bugs

Please include:

- What you did and what you expected.
- What actually happened (with logs — use `/log` or Settings → Logs → Download).
- Your deployment method (Docker, Hugging Face, VPS, …) and app version.

## Pull requests

- Keep changes focused and describe the motivation.
- Match the existing code style.
- Test your change locally before opening the PR.

```{admonition} Beginner Tip
:class: tip
Small, well-described PRs get reviewed and merged faster than large ones that change many
things at once.
```
