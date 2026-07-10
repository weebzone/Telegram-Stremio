# Configuration file for the Sphinx documentation builder.
# Docs: https://www.sphinx-doc.org/en/master/usage/configuration.html

from datetime import datetime

# -- Project information -----------------------------------------------------
project = "Telegram-Stremio"
author = "weebzone & contributors"
copyright = f"{datetime.now().year}, {author}"
release = "latest"

# -- General configuration ---------------------------------------------------
extensions = [
    "myst_parser",
    "sphinx_copybutton",
    "sphinx_design",
]

# MyST (Markdown) features we use across the docs.
myst_enable_extensions = [
    "colon_fence",   # ::: fenced admonitions / directives
    "deflist",       # definition lists
    "linkify",       # auto-link bare URLs
    "substitution",  # variable substitutions
    "tasklist",      # GitHub-style checkboxes
]
myst_heading_anchors = 3

source_suffix = {
    ".md": "markdown",
    ".rst": "restructuredtext",
}

templates_path = ["_templates"]
exclude_patterns = ["_build", "Thumbs.db", ".DS_Store", ".venv", "**/.venv"]

# -- HTML output (Furo theme) ------------------------------------------------
html_theme = "furo"
html_title = "Telegram-Stremio Docs"
html_static_path = ["_static"]
html_css_files = ["custom.css"]

# Show a friendly 404-safe experience and a clean sidebar.
html_theme_options = {
    "sidebar_hide_name": False,
    "navigation_with_keys": True,
    "top_of_page_buttons": ["view", "edit"],
    "source_repository": "https://github.com/weebzone/Telegram-Stremio",
    "source_branch": "master",
    "source_directory": "docs/",
    "light_css_variables": {
        "color-brand-primary": "#B45309",
        "color-brand-content": "#B45309",
    },
    "dark_css_variables": {
        "color-brand-primary": "#F59E0B",
        "color-brand-content": "#FBBF24",
    },
    "footer_icons": [
        {
            "name": "GitHub",
            "url": "https://github.com/weebzone/Telegram-Stremio",
            "html": "",
            "class": "fa-brands fa-github",
        },
    ],
}

# Copybutton: don't copy prompt characters.
copybutton_prompt_text = r">>> |\.\.\. |\$ |# "
copybutton_prompt_is_regexp = True
