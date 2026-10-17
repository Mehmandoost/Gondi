from __future__ import annotations

import pycountry


def normalize_language(value: str | None) -> str | None:
    """Map ISO 639 tags (e.g. en, eng, en-US) to an English display name."""
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None

    tag = text.replace("_", "-")
    primary = tag.split("-")[0]

    if len(primary) == 2:
        lang = pycountry.languages.get(alpha_2=primary.lower())
        if lang and lang.name:
            return lang.name
    if len(primary) == 3 and primary.isalpha():
        lang = pycountry.languages.get(alpha_3=primary.lower())
        if lang and lang.name:
            return lang.name

    return text
