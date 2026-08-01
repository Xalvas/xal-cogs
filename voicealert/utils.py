from __future__ import annotations

import re
import unicodedata
from typing import Protocol


class ChannelNameMember(Protocol):
    """The member attributes used when rendering a room name."""

    id: int
    display_name: str
    name: str


def render_channel_name(
    template: str,
    member: ChannelNameMember,
    *,
    max_length: int,
) -> str:
    """Render and sanitise a Discord voice-channel name."""
    rendered = template
    replacements = {
        "{display_name}": member.display_name,
        "{name}": member.name,
        "{id}": str(member.id),
    }
    for placeholder, value in replacements.items():
        rendered = rendered.replace(placeholder, value)

    # Remove control characters and characters that can create confusing
    # mentions/formatting, then collapse whitespace.
    rendered = "".join(
        character
        for character in rendered
        if unicodedata.category(character) not in {"Cc", "Cf"}
        and character not in "@#`"
    )
    rendered = re.sub(r"\s+", " ", rendered).strip()
    if not rendered:
        rendered = f"Admin Help — {member.id}"
    return rendered[:max_length].rstrip()
