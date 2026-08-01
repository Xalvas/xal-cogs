from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord

if TYPE_CHECKING:
    from .voicealert import VoiceAlert


log = logging.getLogger("red.xalvas.voicealert")


class VoiceSupportRequestView(discord.ui.View):
    """Persistent entry point for requesting a temporary support room."""

    def __init__(self, cog: VoiceAlert) -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="Request Voice Support",
        style=discord.ButtonStyle.primary,
        custom_id="voicealert:request_support",
    )
    async def request_support(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        await self.cog.handle_support_request(interaction)

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item,
    ) -> None:
        """Log unexpected panel failures and give the requester a safe response."""
        log.error(
            "Unhandled VoiceAlert panel error for item %s",
            item.custom_id,
            exc_info=(type(error), error, error.__traceback__),
        )
        try:
            if interaction.response.is_done():
                await interaction.followup.send(
                    "Voice support could not process that request. Please try again.",
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    "Voice support could not process that request. Please try again.",
                    ephemeral=True,
                )
        except discord.HTTPException:
            log.warning("Could not send an error response for a VoiceAlert interaction")


def join_voice_support_view(guild_id: int, channel_id: int) -> discord.ui.View:
    """Build the ephemeral link button for an existing or new room."""
    view = discord.ui.View(timeout=300)
    view.add_item(
        discord.ui.Button(
            label="Join Voice Support",
            style=discord.ButtonStyle.link,
            url=f"https://discord.com/channels/{guild_id}/{channel_id}",
        )
    )
    return view
