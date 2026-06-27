THEMES = {
    "dark_professional": {
        "name": "Dark Professional",
        "is_dark": True,
        "colors": {
            "primary": "#06B6D4",
            "secondary": "#0891B2",
            "accent": "#22D3EE",
            "background": "#0F172A",
            "card": "#1E293B",
            "border": "#334155",
            "text": "#F8FAFC",
            "text_secondary": "#A9B6C8"
        },
        "css_classes": "theme-dark-professional"
    },
    "purple_gradient": {
        "name": "Purple Gradient",
        "is_dark": False,
        "colors": {
            "primary": "#9333EA",
            "secondary": "#7C3AED",
            "accent": "#A855F7",
            "background": "#F6F4FB",
            "card": "#FFFFFF",
            "border": "#E2DCF0",
            "text": "#1E1B2E",
            "text_secondary": "#5B5470"
        },
        "css_classes": "theme-purple-gradient"
    },
    "blue_navy": {
        "name": "Navy Blue",
        "is_dark": False,
        "colors": {
            "primary": "#2563EB",
            "secondary": "#1E3A8A",
            "accent": "#3B82F6",
            "background": "#F1F5F9",
            "card": "#FFFFFF",
            "border": "#CBD5E1",
            "text": "#1E293B",
            "text_secondary": "#475569"
        },
        "css_classes": "theme-blue-navy"
    },
    "cyber_neon": {
        "name": "Cyber Neon",
        "is_dark": True,
        "colors": {
            "primary": "#22D3EE",
            "secondary": "#E935E9",
            "accent": "#34F5A0",
            "background": "#070A18",
            "card": "#10152E",
            "border": "#27305A",
            "text": "#F2F5FF",
            "text_secondary": "#A4AFD0"
        },
        "css_classes": "theme-cyber-neon"
    },
    "midnight_carbon": {
        "name": "Midnight Carbon",
        "is_dark": True,
        "colors": {
            "primary": "#3B82F6",
            "secondary": "#1D4ED8",
            "accent": "#60A5FA",
            "background": "#030712",
            "card": "#111827",
            "border": "#283443",
            "text": "#F9FAFB",
            "text_secondary": "#AEB7C4"
        },
        "css_classes": "theme-midnight-carbon"
    },
    "ocean_mint": {
        "name": "Ocean Mint",
        "is_dark": False,
        "colors": {
            "primary": "#059669",
            "secondary": "#047857",
            "accent": "#0891B2",
            "background": "#F0FDF4",
            "card": "#FFFFFF",
            "border": "#C9EBD6",
            "text": "#064E3B",
            "text_secondary": "#3F5750"
        },
        "css_classes": "theme-ocean-mint"
    },

    "sunset_warm": {
        "name": "Sunset Warm",
        "is_dark": False,
        "colors": {
            "primary": "#EA580C",
            "secondary": "#DC2626",
            "accent": "#DB2777",
            "background": "#FFFBEB",
            "card": "#FFFFFF",
            "border": "#F5E4C3",
            "text": "#451A03",
            "text_secondary": "#7C3A12"
        },
        "css_classes": "theme-sunset-warm"
    },
    "forest_earth": {
        "name": "Forest Earth",
        "is_dark": False,
        "colors": {
            "primary": "#166534",
            "secondary": "#14532D",
            "accent": "#4D7C4F",
            "background": "#F7F7F2",
            "card": "#FFFFFF",
            "border": "#E0E2DA",
            "text": "#14532D",
            "text_secondary": "#4B5043"
        },
        "css_classes": "theme-forest-earth"
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
    }
}

def get_theme(theme_name: str = "dark_professional"):
    """Returns the dictionary for the requested theme or the default."""
    return THEMES.get(theme_name, THEMES["dark_professional"])

def get_all_themes():
    """Returns all available theme configurations."""
    return THEMES
