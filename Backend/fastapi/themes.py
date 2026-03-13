# Updated Theme configurations with deeper UI tokens
THEMES = {
    "purple_gradient": {
        "name": "Purple Gradient",
        "colors": {
            "primary": "#A855F7",
            "secondary": "#7C3AED",
            "accent": "#C084FC",
            "background": "#F8FAFC",
            "card": "#FFFFFF",
            "border": "#E2E8F0",  # Added for card borders
            "text": "#0F172A",     # Deepened for better readability
            "text_secondary": "#64748B"
        },
        "css_classes": "theme-purple-gradient"
    },
    "blue_navy": {
        "name": "Navy Blue",
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
    "dark_professional": {
        "name": "Dark Professional",
        "colors": {
            "primary": "#06B6D4",
            "secondary": "#0891B2",
            "accent": "#22D3EE",
            "background": "#0F172A", # Deep navy background
            "card": "#1E293B",       # Elevated card depth
            "border": "#334155",     # Subtle border for dark mode
            "text": "#F8FAFC",
            "text_secondary": "#94A3B8"
        },
        "css_classes": "theme-dark-professional"
    },
    "cyber_neon": {
        "name": "Cyber Neon",
        "colors": {
            "primary": "#00FFF0",
            "secondary": "#FF00FF",
            "accent": "#00FF88",
            "background": "#050714",
            "card": "#0D1127",
            "border": "#1A1F3A",
            "text": "#FFFFFF",
            "text_secondary": "#8B95B3"
        },
        "css_classes": "theme-cyber-neon"
    },
    "midnight_carbon": {
        "name": "Midnight Carbon",
        "colors": {
            "primary": "#3B82F6",
            "secondary": "#1D4ED8",
            "accent": "#60A5FA",
            "background": "#030712", # Darker "Carbon" base
            "card": "#111827",
            "border": "#1F2937",
            "text": "#F9FAFB",
            "text_secondary": "#9CA3AF"
        },
        "css_classes": "theme-midnight-carbon"
    },
    "ocean_mint": {
        "name": "Ocean Mint",
        "colors": {
            "primary": "#10B981",
            "secondary": "#059669",
            "accent": "#06B6D4",
            "background": "#F0FDF4",
            "card": "#FFFFFF",
            "border": "#DCFCE7",
            "text": "#064E3B",
            "text_secondary": "#374151"
        },
        "css_classes": "theme-ocean-mint"
    },
    # Rest of the themes updated with standard border logic...
    "sunset_warm": {
        "name": "Sunset Warm",
        "colors": {
            "primary": "#F59E0B", "secondary": "#DC2626", "accent": "#EC4899",
            "background": "#FFFBEB", "card": "#FFFFFF", "border": "#FEF3C7",
            "text": "#451A03", "text_secondary": "#78350F"
        },
        "css_classes": "theme-sunset-warm"
    },
    "forest_earth": {
        "name": "Forest Earth",
        "colors": {
            "primary": "#166534", "secondary": "#064E3B", "accent": "#86A789",
            "background": "#F7F7F2", "card": "#FFFFFF", "border": "#E5E7EB",
            "text": "#14532D", "text_secondary": "#4B5563"
        },
        "css_classes": "theme-forest-earth"
    }
}

def get_theme(theme_name: str = "purple_gradient"):
    """Returns the dictionary for the requested theme or the default."""
    return THEMES.get(theme_name, THEMES["purple_gradient"])

def get_all_themes():
    """Returns all available theme configurations."""
    return THEMES
