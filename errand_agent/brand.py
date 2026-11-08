"""The product's name, in one place. Everything the user sees or hears uses these.

Internal identifiers keep the original assignment name ("Errand Agent"): the Python package `errand_agent`,
`ERRAND_*` settings, browser-storage keys and the Sarvam tool id `errand_turn`. Renaming those would break
existing installs and links for no visible change.
"""
import os

NAME = os.environ.get("APP_NAME", "Sorted")
TAGLINE = "Home services, sorted."
SLUG = NAME.lower().replace(" ", "-")  # LiveKit agent / room names
