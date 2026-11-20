DEFAULT_THEME = "paper"

THEMES = {
    "ink": {
        "name": "Ink",
        "is_dark": True,
        "colors": {
            "primary": "#E9A23B",
            "on_primary": "#1A1405",
            "background": "#121212",
            "card": "#1A1A19",
            "card_high": "#232321",
            "border": "#2E2D2A",
            "text": "#EFECE6",
            "text_secondary": "#8F8A82",
            "danger": "#E5534B",
            "ok": "#7BB662",
        },
    },
    "paper": {
        "name": "Paper",
        "is_dark": False,
        "colors": {
            "primary": "#B9471A",
            "on_primary": "#FFFFFF",
            "background": "#F3F0E8",
            "card": "#FBF9F4",
            "card_high": "#EAE5DA",
            "border": "#DCD5C7",
            "text": "#1D1B18",
            "text_secondary": "#6E685E",
            "danger": "#B42318",
            "ok": "#3F7D2C",
        },
    },
    "night": {
        "name": "Night",
        "is_dark": True,
        "colors": {
            "primary": "#8CC4AE",
            "on_primary": "#06140F",
            "background": "#000000",
            "card": "#0B0B0B",
            "card_high": "#151515",
            "border": "#222222",
            "text": "#E6E6E6",
            "text_secondary": "#7C7C7C",
            "danger": "#E0625A",
            "ok": "#8CC4AE",
        },
    },
}


def get_theme(theme_name: str = DEFAULT_THEME):
    return THEMES.get(theme_name, THEMES[DEFAULT_THEME])


def get_all_themes():
    return THEMES


def theme_key(request) -> str:
    key = request.cookies.get("theme") or request.session.get("theme")
    return key if key in THEMES else DEFAULT_THEME
