from __future__ import annotations

import asyncio
import logging
from typing import Final, Optional

import discord
from redbot.core import Config, commands
from redbot.core.bot import Red

from .room_manager import RoomManager
from .validation import (
    configuration_issues,
    missing_permissions,
    unpingable_roles,
    valid_roles,
)
from .views import VoiceSupportRequestView


log = logging.getLogger("red.xalvas.voicealert")


class VoiceAlert(commands.Cog):
    """Create private, temporary voice rooms for support requests."""

    __author__: Final[list[str]] = ["Xalvas"]
    __version__: Final[str] = "0.4.1"
    SCHEMA_VERSION: Final[int] = 2
    MAX_CHANNEL_NAME_LENGTH: Final[int] = 100
    MAX_COOLDOWN_SECONDS: Final[int] = 86400
    ORPHAN_RETRY_SECONDS: Final[int] = 30

    def __init__(self, bot: Red) -> None:
        self.bot = bot
        self.config = Config.get_conf(
            self,
            identifier=981274630,
            force_registration=True,
        )
        self.config.register_guild(
            enabled=False,
            panel_channel_id=None,
            panel_message_id=None,
            category_id=None,
            alert_channel_id=None,
            support_role_ids=[],
            ping_role_ids=[],
            creation_cooldown_seconds=60,
            empty_delete_delay_seconds=10,
            initial_join_timeout_seconds=300,
            channel_name_template="Admin Help — {display_name}",
            active_rooms={},
            schema_version=0,
        )

        self.room_manager = RoomManager(self)
        self._persistent_view = VoiceSupportRequestView(self)
        self._deployed_views: list[VoiceSupportRequestView] = []
        self._active_operation_tasks: set[asyncio.Task[None]] = set()
        self._unloading = False
        self._unload_task: Optional[asyncio.Task[None]] = None

    def _register_operation(self) -> tuple[bool, Optional[asyncio.Task[None]]]:
        """Register the current producer task unless cog unload has started."""
        current_task = asyncio.current_task()
        if self._unloading or current_task is self._unload_task:
            return False, None
        if current_task is not None:
            if current_task in self._active_operation_tasks:
                return True, None
            self._active_operation_tasks.add(current_task)
        return True, current_task

    def _unregister_operation(
        self, operation_task: Optional[asyncio.Task[None]]
    ) -> None:
        """Forget a producer task after its operation has fully completed."""
        if operation_task is not None:
            self._active_operation_tasks.discard(operation_task)

    async def cog_load(self) -> None:
        """Register the persistent view and migrate stored configuration."""
        self._unloading = False
        self._unload_task = None
        await self._migrate_config()
        self.bot.add_view(self._persistent_view)
        if self.bot.is_ready():
            await self.room_manager._restore_managed_rooms()
        else:
            self.room_manager._schedule_recovery_after_ready()

    async def cog_unload(self) -> None:
        """Stop views and await cancellation of every background task."""
        self._unload_task = asyncio.current_task()
        self._unloading = True
        try:
            self._persistent_view.stop()
        except Exception:
            log.exception("Failed to unregister the VoiceAlert persistent view")
        for view in self._deployed_views:
            try:
                view.stop()
            except Exception:
                log.exception("Failed to stop a deployed VoiceAlert view")
        self._deployed_views.clear()

        # Let operations dispatched immediately before their entry points were
        # removed register themselves, then drain every pre-unload producer.
        current_task = self._unload_task
        while True:
            await asyncio.sleep(0)
            active_operations = [
                task
                for task in self._active_operation_tasks
                if task is not current_task and not task.done()
            ]
            if not active_operations:
                break
            await asyncio.gather(*active_operations, return_exceptions=True)
        self._active_operation_tasks.clear()

        await self.room_manager.shutdown_owned_tasks(current_task)

    def format_help_for_context(self, ctx: commands.Context) -> str:
        """Include the cog version in its help page."""
        help_text = super().format_help_for_context(ctx)
        return f"{help_text}\n\nVersion: {self.__version__}"

    async def _migrate_config(self) -> None:
        """Disable legacy configurations and mark them with the current schema."""
        all_guilds = await self.config.all_guilds()
        for guild_id, stored in all_guilds.items():
            old_version = stored.get("schema_version", 0)
            if old_version >= self.SCHEMA_VERSION:
                continue

            guild_config = self.config.guild_from_id(guild_id)
            if old_version < 1:
                # A legacy join-alert setup is not safe to run as a room creator.
                await guild_config.enabled.set(False)
            await guild_config.schema_version.set(self.SCHEMA_VERSION)

    async def handle_support_request(self, interaction: discord.Interaction) -> None:
        """Delegate a panel callback to the active room manager."""
        await self.room_manager.handle_support_request(interaction)

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        """Schedule deletion after the last member leaves a managed room."""
        should_run, operation_task = self._register_operation()
        if not should_run:
            return
        try:
            try:
                await self.room_manager._handle_voice_state_update(
                    member, before, after
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception(
                    "Unexpected error processing a voice-state update in guild %s",
                    member.guild.id,
                )
        finally:
            self._unregister_operation(operation_task)

    @commands.Cog.listener()
    async def on_guild_channel_delete(
        self, channel: discord.abc.GuildChannel
    ) -> None:
        """Remove records for managed rooms that were deleted manually."""
        should_run, operation_task = self._register_operation()
        if not should_run:
            return
        try:
            await self.room_manager._handle_guild_channel_delete(channel)
        finally:
            self._unregister_operation(operation_task)

    @commands.hybrid_group(
        name="voicealert",
        aliases=["valert"],
        invoke_without_command=True,
    )
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def voicealert(self, ctx: commands.Context) -> None:
        """Configure temporary voice support rooms."""
        await ctx.send_help(ctx.command)

    @voicealert.command(name="status")
    async def voicealert_status(self, ctx: commands.Context) -> None:
        """Display the current VoiceAlert configuration."""
        guild = ctx.guild
        settings = await self.config.guild(guild).all()
        panel = guild.get_channel(settings["panel_channel_id"])
        category = guild.get_channel(settings["category_id"])
        alert = guild.get_channel(settings["alert_channel_id"])
        support_roles = valid_roles(guild, settings["support_role_ids"])
        ping_roles = valid_roles(guild, settings["ping_role_ids"])
        issues = configuration_issues(guild, settings)

        embed = discord.Embed(title="VoiceAlert Configuration", colour=await ctx.embed_colour())
        embed.add_field(
            name="Status", value="Enabled" if settings["enabled"] else "Disabled"
        )
        embed.add_field(
            name="Configuration", value="Complete" if not issues else "Incomplete"
        )
        embed.add_field(name="Active rooms", value=str(len(settings["active_rooms"])))
        embed.add_field(
            name="Panel channel",
            value=panel.mention if isinstance(panel, discord.TextChannel) else "Not configured",
            inline=False,
        )
        embed.add_field(
            name="Panel message ID",
            value=str(settings["panel_message_id"] or "Not deployed"),
            inline=False,
        )
        embed.add_field(
            name="Room category",
            value=category.name if isinstance(category, discord.CategoryChannel) else "Not configured",
            inline=False,
        )
        embed.add_field(
            name="Alert channel",
            value=alert.mention if isinstance(alert, discord.TextChannel) else "Not configured",
            inline=False,
        )
        embed.add_field(
            name="Support roles",
            value=" ".join(role.mention for role in support_roles) or "Not configured",
            inline=False,
        )
        embed.add_field(
            name="Ping roles",
            value=" ".join(role.mention for role in ping_roles) or "Not configured",
            inline=False,
        )
        embed.add_field(
            name="Creation cooldown",
            value=f"{settings['creation_cooldown_seconds']} seconds",
        )
        embed.add_field(
            name="Empty-room delay",
            value=f"{settings['empty_delete_delay_seconds']} seconds",
        )
        embed.add_field(
            name="Initial join timeout",
            value=f"{settings['initial_join_timeout_seconds']} seconds",
        )
        embed.add_field(
            name="Channel name template",
            value=f"`{settings['channel_name_template']}`",
            inline=False,
        )
        if issues:
            embed.add_field(
                name="Still required",
                value="\n".join(f"• {issue}" for issue in issues),
                inline=False,
            )
        await ctx.send(
            embed=embed,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @voicealert.command(name="setpanelchannel")
    async def voicealert_setpanelchannel(
        self, ctx: commands.Context, channel: discord.TextChannel
    ) -> None:
        """Set the text channel used for the request panel."""
        bot_member = ctx.guild.me
        if bot_member is None:
            await ctx.send("I could not resolve my server member to check permissions.")
            return
        missing = missing_permissions(
            channel,
            bot_member,
            (
                ("view_channel", "View Channel"),
                ("send_messages", "Send Messages"),
                ("embed_links", "Embed Links"),
            ),
        )
        if missing:
            await ctx.send(
                f"I cannot use {channel.mention} for the panel; I am missing: "
                f"**{', '.join(missing)}**."
            )
            return
        await self.config.guild(ctx.guild).panel_channel_id.set(channel.id)
        await self.config.guild(ctx.guild).panel_message_id.set(None)
        await ctx.send(f"The VoiceAlert panel channel is now {channel.mention}.")

    @voicealert.command(name="setcategory")
    async def voicealert_setcategory(
        self, ctx: commands.Context, category: discord.CategoryChannel
    ) -> None:
        """Set the category where temporary rooms are created."""
        bot_member = ctx.guild.me
        if bot_member is None:
            await ctx.send("I could not resolve my server member to check permissions.")
            return
        missing = missing_permissions(
            category,
            bot_member,
            (
                ("view_channel", "View Channel"),
                ("manage_channels", "Manage Channels"),
                ("connect", "Connect"),
                ("move_members", "Move Members"),
            ),
        )
        if missing:
            await ctx.send(
                f"I cannot use **{category.name}**; I am missing: "
                f"**{', '.join(missing)}**."
            )
            return
        await self.config.guild(ctx.guild).category_id.set(category.id)
        await ctx.send(f"Voice support rooms will be created under **{category.name}**.")

    @voicealert.command(name="setalertchannel")
    async def voicealert_setalertchannel(
        self, ctx: commands.Context, channel: discord.TextChannel
    ) -> None:
        """Set the text channel that receives support alerts."""
        bot_member = ctx.guild.me

        if bot_member is None:
            await ctx.send("I could not resolve my server member to check permissions.")
            return
        missing = missing_permissions(
            channel,
            bot_member,
            (
                ("view_channel", "View Channel"),
                ("send_messages", "Send Messages"),
                ("embed_links", "Embed Links"),
            ),
        )
        if missing:
            await ctx.send(
                f"I cannot use {channel.mention} for alerts; I am missing: "
                f"**{', '.join(missing)}**."
            )
            return
        settings = await self.config.guild(ctx.guild).all()
        unpingable = unpingable_roles(ctx.guild, settings, channel)
        if unpingable:
            await ctx.send(
                "I cannot use that alert channel because these ping roles are not "
                "mentionable and I lack **Mention Everyone** there: "
                + ", ".join(role.mention for role in unpingable)
            )
            return
        await self.config.guild(ctx.guild).alert_channel_id.set(channel.id)
        await ctx.send(f"Voice support alerts will be sent to {channel.mention}.")

    async def _add_role(
        self, ctx: commands.Context, role: discord.Role, setting: str, label: str
    ) -> None:
        if role.is_default():
            await ctx.send(f"The `@everyone` role cannot be added as a {label} role.")
            return
        config_value = getattr(self.config.guild(ctx.guild), setting)
        async with config_value() as role_ids:
            if role.id in role_ids:
                await ctx.send(f"{role.mention} is already a {label} role.")
                return
            role_ids.append(role.id)
        await ctx.send(f"Added {role.mention} as a VoiceAlert {label} role.")

    async def _remove_role(
        self, ctx: commands.Context, role: discord.Role, setting: str, label: str
    ) -> None:
        config_value = getattr(self.config.guild(ctx.guild), setting)
        async with config_value() as role_ids:
            if role.id not in role_ids:
                await ctx.send(f"{role.mention} is not a configured {label} role.")
                return
            role_ids.remove(role.id)
        await ctx.send(f"Removed {role.mention} from the VoiceAlert {label} roles.")

    @voicealert.command(name="addsupportrole")
    async def voicealert_addsupportrole(
        self, ctx: commands.Context, role: discord.Role
    ) -> None:
        """Give a role access to temporary support rooms."""
        await self._add_role(ctx, role, "support_role_ids", "support")

    @voicealert.command(name="removesupportrole")
    async def voicealert_removesupportrole(
        self, ctx: commands.Context, role: discord.Role
    ) -> None:
        """Remove a role's access to newly created support rooms."""
        await self._remove_role(ctx, role, "support_role_ids", "support")

    @voicealert.command(name="addpingrole")
    async def voicealert_addpingrole(
        self, ctx: commands.Context, role: discord.Role
    ) -> None:
        """Add a role to support-request alert pings."""
        if role.is_default():
            await ctx.send("The `@everyone` role cannot be added as a ping role.")
            return
        alert_channel_id = await self.config.guild(ctx.guild).alert_channel_id()
        alert_channel = ctx.guild.get_channel(alert_channel_id)
        bot_member = ctx.guild.me
        can_mention_all = bool(
            isinstance(alert_channel, discord.TextChannel)
            and bot_member is not None
            and alert_channel.permissions_for(bot_member).mention_everyone
        )
        if not role.mentionable and not can_mention_all:
            await ctx.send(
                f"{role.mention} is not mentionable, and I do not have "
                "**Mention Everyone** in the configured alert channel."
            )
            return
        await self._add_role(ctx, role, "ping_role_ids", "ping")

    @voicealert.command(name="removepingrole")
    async def voicealert_removepingrole(
        self, ctx: commands.Context, role: discord.Role
    ) -> None:
        """Remove a role from support-request alert pings."""
        await self._remove_role(ctx, role, "ping_role_ids", "ping")

    @voicealert.command(name="cooldown")
    async def voicealert_cooldown(self, ctx: commands.Context, seconds: int) -> None:
        """Set the per-user room creation cooldown in seconds."""
        if not 0 <= seconds <= 86400:
            await ctx.send("Cooldown must be between 0 and 86,400 seconds.")
            return
        await self.config.guild(ctx.guild).creation_cooldown_seconds.set(seconds)
        await ctx.send(f"The room creation cooldown is now **{seconds} seconds**.")

    @voicealert.command(name="deletetime")
    async def voicealert_deletetime(self, ctx: commands.Context, seconds: int) -> None:
        """Set how long an empty support room remains before deletion."""
        if not 0 <= seconds <= 86400:
            await ctx.send("Delete time must be between 0 and 86,400 seconds.")
            return
        await self.config.guild(ctx.guild).empty_delete_delay_seconds.set(seconds)
        await ctx.send(f"Empty rooms will be deleted after **{seconds} seconds**.")

    @voicealert.command(name="jointimeout")
    async def voicealert_jointimeout(
        self, ctx: commands.Context, seconds: int
    ) -> None:
        """Set how long a new, never-occupied room waits for its requester."""
        if not 30 <= seconds <= 3600:
            await ctx.send("Initial join timeout must be between 30 and 3,600 seconds.")
            return
        await self.config.guild(ctx.guild).initial_join_timeout_seconds.set(seconds)
        await ctx.send(
            f"New rooms will wait **{seconds} seconds** for their first occupant."
        )

    @voicealert.command(name="setname")
    async def voicealert_setname(self, ctx: commands.Context, *, template: str) -> None:
        """Set the room name template; supports display_name, name, and id fields."""
        if not template.strip() or len(template) > 200:
            await ctx.send("The template must contain 1 to 200 characters.")
            return
        await self.config.guild(ctx.guild).channel_name_template.set(template.strip())
        await ctx.send(
            "The room name template is now "
            f"`{discord.utils.escape_markdown(template.strip())}`."
        )

    @voicealert.command(name="enable")
    async def voicealert_enable(self, ctx: commands.Context) -> None:
        """Enable room requests after validating the configuration."""
        settings = await self.config.guild(ctx.guild).all()
        issues = configuration_issues(ctx.guild, settings)
        if issues:
            await ctx.send(
                "VoiceAlert cannot be enabled yet. Configure:\n"
                + "\n".join(f"- {issue}" for issue in issues)
            )
            return
        await self.config.guild(ctx.guild).support_role_ids.set(
            [role.id for role in valid_roles(ctx.guild, settings["support_role_ids"])]
        )
        await self.config.guild(ctx.guild).ping_role_ids.set(
            [role.id for role in valid_roles(ctx.guild, settings["ping_role_ids"])]
        )
        await self.config.guild(ctx.guild).schema_version.set(self.SCHEMA_VERSION)
        await self.config.guild(ctx.guild).enabled.set(True)
        await ctx.send("VoiceAlert has been **enabled**.")

    @voicealert.command(name="disable")
    async def voicealert_disable(self, ctx: commands.Context) -> None:
        """Disable new requests without deleting configuration or active rooms."""
        await self.config.guild(ctx.guild).enabled.set(False)
        await ctx.send("VoiceAlert has been **disabled**.")

    @voicealert.command(name="deploy")
    async def voicealert_deploy(self, ctx: commands.Context) -> None:
        """Deploy the persistent VoiceAlert request panel."""
        should_run, operation_task = self._register_operation()
        if not should_run:
            await ctx.send("VoiceAlert is reloading. Please try again in a moment.")
            return
        panel_view: Optional[VoiceSupportRequestView] = None
        try:
            channel_id = await self.config.guild(ctx.guild).panel_channel_id()
            channel = ctx.guild.get_channel(channel_id)
            if not isinstance(channel, discord.TextChannel):
                await ctx.send("Configure a valid panel channel first.")
                return
            bot_member = ctx.guild.me
            if bot_member is None:
                await ctx.send("I could not resolve my server member to check permissions.")
                return
            permissions = channel.permissions_for(bot_member)
            missing = []
            for attribute, label in (
                ("view_channel", "View Channel"),
                ("send_messages", "Send Messages"),
                ("embed_links", "Embed Links"),
            ):
                if not getattr(permissions, attribute):
                    missing.append(label)
            if missing:
                await ctx.send(
                    f"I cannot deploy in {channel.mention}; I am missing: "
                    f"**{', '.join(missing)}**."
                )
                return

            embed = discord.Embed(
                title="Voice Support",
                description=(
                    "Need help in voice? Use the button below to create a private "
                    "temporary room for you and the support team."
                ),
                colour=await ctx.embed_colour(),
            )
            panel_view = VoiceSupportRequestView(self)
            try:
                message = await channel.send(embed=embed, view=panel_view)
            except discord.Forbidden:
                panel_view.stop()
                await ctx.send("Discord denied permission to deploy the VoiceAlert panel.")
                return
            except discord.HTTPException:
                panel_view.stop()
                log.exception("Failed to deploy VoiceAlert panel in guild %s", ctx.guild.id)
                await ctx.send("Discord could not deploy the panel. Please try again.")
                return
            self._deployed_views.append(panel_view)
            await self.config.guild(ctx.guild).panel_message_id.set(message.id)
            await ctx.send(f"VoiceAlert panel deployed in {channel.mention}.")
        finally:
            if self._unloading and panel_view is not None:
                panel_view.stop()
            self._unregister_operation(operation_task)

    @voicealert.command(name="cleanup")
    async def voicealert_cleanup(
        self, ctx: commands.Context, delete_empty: bool = True
    ) -> None:
        """Prune stale records and optionally delete empty managed rooms."""
        should_run, operation_task = self._register_operation()
        if not should_run:
            await ctx.send("VoiceAlert is reloading. Please try again in a moment.")
            return
        try:
            await self.room_manager._run_voicealert_cleanup(ctx, delete_empty)
        finally:
            self._unregister_operation(operation_task)

    @voicealert.command(name="reset")
    async def voicealert_reset(
        self, ctx: commands.Context, confirmation: bool = False
    ) -> None:
        """Reset all VoiceAlert settings; pass true to confirm."""
        should_run, operation_task = self._register_operation()
        if not should_run:
            await ctx.send("VoiceAlert is reloading. Please try again in a moment.")
            return
        try:
            await self.room_manager._run_voicealert_reset(ctx, confirmation)
        finally:
            self._unregister_operation(operation_task)
