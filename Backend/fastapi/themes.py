DEFAULT_THEME = "graphite_amber"

THEMES = {
    "graphite_amber": {
        "name": "Graphite Amber",
        "is_dark": True,
        "colors": {
            "primary": "#F59E0B",
            "secondary": "#D97706",
            "accent": "#FBBF24",
            "background": "#0C0C0D",
            "card": "#18181B",
            "border": "#2B2B30",
            "text": "#FAFAF9",
            "text_secondary": "#A9A8A4"
        },
        "css_classes": "theme-graphite-amber"
    },
    "amoled_midnight": {
        "name": "AMOLED Midnight",
        "is_dark": True,
        "colors": {
            "primary": "#38BDF8",
            "secondary": "#0EA5E9",
            "accent": "#818CF8",
            "background": "#000000",
            "card": "#0B0B0F",
            "border": "#1E2430",
            "text": "#F4F7FB",
            "text_secondary": "#9AA6B8"
        },
        "css_classes": "theme-amoled-midnight"
    },
    "obsidian_emerald": {
        "name": "Obsidian Emerald",
        "is_dark": True,
        "colors": {
            "primary": "#10B981",
            "secondary": "#059669",
            "accent": "#34D399",
            "background": "#08100C",
            "card": "#111C16",
            "border": "#203029",
            "text": "#ECFDF5",
            "text_secondary": "#92B6A4"
        },
        "css_classes": "theme-obsidian-emerald"
    },
    "royal_violet": {
        "name": "Royal Violet",
        "is_dark": True,
        "colors": {
            "primary": "#8B5CF6",
            "secondary": "#7C3AED",
            "accent": "#A78BFA",
            "background": "#0B0712",
            "card": "#171022",
            "border": "#2C2142",
            "text": "#F5F3FF",
            "text_secondary": "#B4A8CF"
        },
        "css_classes": "theme-royal-violet"
    },
    "slate_ocean": {
        "name": "Slate Ocean",
        "is_dark": True,
        "colors": {
            "primary": "#0EA5E9",
            "secondary": "#0284C7",
            "accent": "#38BDF8",
            "background": "#0A0F1A",
            "card": "#121C2B",
            "border": "#23324B",
            "text": "#EFF6FF",
            "text_secondary": "#93A7C4"
        },
        "css_classes": "theme-slate-ocean"
    },
    "rose_quartz": {
        "name": "Rose Quartz",
        "is_dark": False,
        "colors": {
            "primary": "#E11D48",
            "secondary": "#BE123C",
            "accent": "#F43F5E",
            "background": "#FFF1F2",
            "card": "#FFFFFF",
            "border": "#FBD5DB",
            "text": "#4C0519",
            "text_secondary": "#8A2B3E"
        },
        "css_classes": "theme-rose-quartz"
    },
    "daylight_sky": {
        "name": "Daylight Sky",
        "is_dark": False,
        "colors": {
            "primary": "#2563EB",
            "secondary": "#1D4ED8",
            "accent": "#3B82F6",
            "background": "#F8FAFC",
            "card": "#FFFFFF",
            "border": "#E2E8F0",
            "text": "#0F172A",
            "text_secondary": "#475569"
        },
        "css_classes": "theme-daylight-sky"
    },
    "sage_linen": {
        "name": "Sage Linen",
        "is_dark": False,
        "colors": {
            "primary": "#0F766E",
            "secondary": "#0D9488",
            "accent": "#14B8A6",
            "background": "#F5F8F6",
            "card": "#FFFFFF",
            "border": "#DCE7E3",
            "text": "#11271F",
            "text_secondary": "#4B5D58"
        },
        "css_classes": "theme-sage-linen"
    },
    "golden_hour": {
        "name": "Golden Hour",
        "is_dark": False,
        "colors": {
            "primary": "#B45309",
            "secondary": "#92400E",
            "accent": "#D97706",
            "background": "#FFFBF2",
            "card": "#FFFFFF",
            "border": "#F0E4CC",
            "text": "#3F2D12",
            "text_secondary": "#7A5A2E"
        },
        "css_classes": "theme-golden-hour"
    }
}

#----- Resolve a theme by name, falling back to the default
def get_theme(theme_name: str = DEFAULT_THEME):
    return THEMES.get(theme_name, THEMES[DEFAULT_THEME])


#----- Return the full theme registry
def get_all_themes():
    return THEMES
