
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Optional

import discord
from redbot.core import commands

from .utils import MAX_CHANNEL_NAME_LENGTH, render_channel_name
from .validation import configuration_issues, missing_permissions, valid_roles
from .views import join_voice_support_view

if TYPE_CHECKING:
    from .voicealert import VoiceAlert


log = logging.getLogger("red.xalvas.voicealert")


@dataclass
class RequestLockEntry:
    """A per-requester lock with an explicit waiter/owner reference count."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


class RoomManager:
    """Own and coordinate the temporary voice-room lifecycle."""

    def __init__(self, cog: VoiceAlert) -> None:
        self.cog = cog
        self._deletion_tasks: dict[int, asyncio.Task[None]] = {}
        self._orphan_deletion_tasks: dict[int, asyncio.Task[None]] = {}
        self._recovery_tasks: set[asyncio.Task[None]] = set()
        self._orphan_rooms: dict[int, tuple[int, int]] = {}
        self._creation_locks: dict[tuple[int, int], RequestLockEntry] = {}
        self._guild_operation_locks: dict[int, asyncio.Lock] = {}
        self._last_created_at: dict[tuple[int, int], float] = {}

    async def shutdown_owned_tasks(
        self, current_task: Optional[asyncio.Task[None]]
    ) -> None:
        """Cancel, await, and clear every room-lifecycle task and state object."""
        # All scheduling helpers now refuse new work. Repeat the owned-task
        # drain so completion callbacks cannot leave a task behind.
        while True:
            owned_tasks = set(self._deletion_tasks.values())
            owned_tasks.update(self._orphan_deletion_tasks.values())
            owned_tasks.update(self._recovery_tasks)
            if current_task is not None:
                owned_tasks.discard(current_task)
            if not owned_tasks:
                break
            for task in owned_tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*owned_tasks, return_exceptions=True)
            for channel_id, task in list(self._deletion_tasks.items()):
                if task in owned_tasks:
                    self._deletion_tasks.pop(channel_id, None)
            for channel_id, task in list(self._orphan_deletion_tasks.items()):
                if task in owned_tasks:
                    self._orphan_deletion_tasks.pop(channel_id, None)
            self._recovery_tasks.difference_update(owned_tasks)

        self._deletion_tasks.clear()
        self._orphan_deletion_tasks.clear()
        self._recovery_tasks.clear()
        self._orphan_rooms.clear()
        self._creation_locks.clear()
        self._guild_operation_locks.clear()
        self._last_created_at.clear()

    def _schedule_recovery_after_ready(self) -> None:
        """Start the owned room-recovery task unless unload has begun."""
        if self.cog._unloading:
            log.warning("Skipping VoiceAlert room recovery scheduling during unload")
            return
        recovery_task = asyncio.create_task(
            self._restore_rooms_after_ready(),
            name="voicealert-restore-rooms",
        )
        self._recovery_tasks.add(recovery_task)
        recovery_task.add_done_callback(self._recovery_tasks.discard)

    async def _restore_managed_rooms(self) -> None:
        """Restore cleanup state for persisted rooms after load or restart."""
        should_run, operation_task = self.cog._register_operation()
        if not should_run:
            return
        try:
            await self._restore_managed_rooms_impl()
        finally:
            self.cog._unregister_operation(operation_task)

    async def _restore_managed_rooms_impl(self) -> None:
        """Restore persisted rooms while registered as an active operation."""
        for guild in self.cog.bot.guilds:
            try:
                rooms = await self.cog.config.guild(guild).active_rooms()
            except Exception:
                log.exception(
                    "Failed to read active VoiceAlert rooms for guild %s", guild.id
                )
                continue

            for requester_id, room_record in list(rooms.items()):
                channel_id = room_record.get("channel_id")
                try:
                    channel = guild.get_channel(channel_id)
                    if not isinstance(channel, discord.VoiceChannel):
                        await self._remove_active_room_if_matches(
                            guild, requester_id, channel_id
                        )
                        continue

                    occupied = bool(room_record.get("has_been_occupied", False))
                    if channel.members:
                        occupied = True
                    if (
                        "has_been_occupied" not in room_record
                        or occupied != room_record.get("has_been_occupied")
                    ):
                        await self._set_room_occupied(
                            guild, requester_id, channel.id, occupied
                        )
                        room_record["has_been_occupied"] = occupied

                    await self._schedule_managed_cleanup(
                        guild, requester_id, channel, room_record
                    )
                except Exception:
                    log.exception(
                        "Failed to restore VoiceAlert room: guild=%s requester=%s "
                        "channel=%s",
                        guild.id,
                        requester_id,
                        channel_id,
                    )

    async def _restore_rooms_after_ready(self) -> None:
        """Wait for the guild cache before restoring persisted room timers."""
        try:
            await self.cog.bot.wait_until_ready()
            await self._restore_managed_rooms()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Unexpected failure restoring VoiceAlert rooms after ready")

    async def _send_room_response(
        self,
        interaction: discord.Interaction,
        channel: discord.VoiceChannel,
        *,
        created: bool,
    ) -> None:
        """Send an ephemeral response containing the room link."""
        verb = "created" if created else "already have"
        content = f"You {verb} a voice support room: {channel.mention}"
        view = join_voice_support_view(channel.guild.id, channel.id)
        if interaction.response.is_done():
            await interaction.followup.send(content, view=view, ephemeral=True)
        else:
            await interaction.response.send_message(content, view=view, ephemeral=True)

    async def _remove_active_room_if_matches(
        self,
        guild: discord.Guild,
        requester_id: object,
        expected_channel_id: object,
    ) -> bool:
        """Remove a requester record only when it still points to one channel."""
        key = str(requester_id)
        async with self.cog.config.guild(guild).active_rooms() as rooms:
            room = rooms.get(key)
            if room is None or room.get("channel_id") != expected_channel_id:
                return False

            del rooms[key]
            return True

    async def _set_room_occupied(
        self,
        guild: discord.Guild,
        requester_id: object,
        expected_channel_id: int,
        occupied: bool = True,
    ) -> bool:
        """Update occupancy only if the requester still owns the expected room."""
        key = str(requester_id)
        async with self.cog.config.guild(guild).active_rooms() as rooms:
            room = rooms.get(key)
            if room is None or room.get("channel_id") != expected_channel_id:
                return False
            room["has_been_occupied"] = occupied
            return True

    async def _managed_room_record(
        self, guild: discord.Guild, channel_id: int
    ) -> Optional[tuple[str, dict]]:
        """Find an active-room record by voice-channel ID."""
        rooms = await self.cog.config.guild(guild).active_rooms()
        for requester_id, room in rooms.items():
            if room.get("channel_id") == channel_id:
                return requester_id, room
        return None

    async def _existing_room(
        self, guild: discord.Guild, requester_id: int
    ) -> Optional[discord.VoiceChannel]:
        """Resolve a requester's room, pruning its record when stale."""
        rooms = await self.cog.config.guild(guild).active_rooms()
        room = rooms.get(str(requester_id))
        if room is None:
            return None
        channel = guild.get_channel(room.get("channel_id"))
        if isinstance(channel, discord.VoiceChannel):
            return channel
        await self._remove_active_room_if_matches(
            guild, requester_id, room.get("channel_id")
        )
        return None

    def _prune_cooldown_cache(
        self, now: float, guild_id: int, guild_cooldown: int
    ) -> None:
        """Remove cooldown entries once they can no longer affect a request."""
        for cache_key, created_at in list(self._last_created_at.items()):
            age = now - created_at
            if age >= self.cog.MAX_COOLDOWN_SECONDS or (
                cache_key[0] == guild_id and age >= guild_cooldown
            ):
                self._last_created_at.pop(cache_key, None)

    async def handle_support_request(self, interaction: discord.Interaction) -> None:
        """Track one panel callback through completion during cog shutdown."""
        guild_id: Optional[int] = None
        requester_id: Optional[int] = None
        interaction_channel_id: Optional[int] = None
        should_run, operation_task = self.cog._register_operation()
        try:
            guild_id = interaction.guild_id
            requester_id = getattr(interaction.user, "id", None)
            interaction_channel_id = interaction.channel_id
            if not should_run:
                message = "Voice support is reloading. Please try again in a moment."
                try:
                    if interaction.response.is_done():
                        await interaction.followup.send(message, ephemeral=True)
                    else:
                        await interaction.response.send_message(message, ephemeral=True)
                except discord.HTTPException:
                    log.warning(
                        "Could not acknowledge a VoiceAlert request during unload: "
                        "guild=%s requester=%s channel=%s",
                        guild_id,
                        requester_id,
                        interaction_channel_id,
                    )
                return

            await self._handle_support_request(interaction)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception(
                "Unexpected VoiceAlert request callback failure: guild=%s "
                "requester=%s channel=%s",
                guild_id,
                requester_id,
                interaction_channel_id,
            )
            raise
        finally:
            self.cog._unregister_operation(operation_task)

    async def _handle_support_request(self, interaction: discord.Interaction) -> None:
        """Create or return a private temporary voice support room."""
        if interaction.user.bot:
            await interaction.response.send_message(
                "Bot accounts cannot request voice support.", ephemeral=True
            )
            return
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "Voice support can only be requested from a server.", ephemeral=True
            )
            return

        guild = interaction.guild
        member = interaction.user
        key = (guild.id, member.id)
        lock_entry = self._creation_locks.get(key)
        if lock_entry is None:
            lock_entry = RequestLockEntry()
            self._creation_locks[key] = lock_entry
        lock_entry.users += 1
        guild_lock = self._guild_operation_locks.setdefault(
            guild.id, asyncio.Lock()
        )

        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
            async with lock_entry.lock, guild_lock:
                settings = await self.cog.config.guild(guild).all()
                if not settings["enabled"]:
                    await interaction.followup.send(
                        "Voice support requests are currently disabled.", ephemeral=True
                    )
                    return

                existing = await self._existing_room(guild, member.id)
                if existing is not None:
                    await self._send_room_response(interaction, existing, created=False)
                    return

                issues = configuration_issues(guild, settings)
                if issues:
                    log.warning(
                        "VoiceAlert is enabled with incomplete configuration in guild %s: %s",
                        guild.id,
                        ", ".join(issues),
                    )
                    await interaction.followup.send(
                        "Voice support is temporarily unavailable because its configuration "
                        "is incomplete. Please contact an administrator.",
                        ephemeral=True,
                    )
                    return

                cooldown = max(0, settings["creation_cooldown_seconds"])
                now = time.monotonic()
                self._prune_cooldown_cache(now, guild.id, cooldown)
                last_created = self._last_created_at.get(key)
                if last_created is not None and now - last_created < cooldown:
                    remaining = max(1, int(cooldown - (now - last_created) + 0.999))
                    await interaction.followup.send(
                        f"Please wait **{remaining} seconds** before creating another room.",
                        ephemeral=True,
                    )
                    return

                category = guild.get_channel(settings["category_id"])
                alert_channel = guild.get_channel(settings["alert_channel_id"])
                assert isinstance(category, discord.CategoryChannel)
                assert isinstance(alert_channel, discord.TextChannel)

                bot_member = guild.me
                if bot_member is None:
                    await interaction.followup.send(
                        "Voice support is temporarily unavailable.", ephemeral=True
                    )
                    return
                missing_category_permissions = missing_permissions(
                    category,
                    bot_member,
                    (
                        ("view_channel", "View Channel"),
                        ("manage_channels", "Manage Channels"),
                        ("connect", "Connect"),
                        ("move_members", "Move Members"),
                    ),
                )
                if missing_category_permissions:
                    await interaction.followup.send(
                        "I cannot create a support room because I am missing these "
                        "category permissions: "
                        f"**{', '.join(missing_category_permissions)}**.",
                        ephemeral=True,
                    )
                    return

                missing_alert_permissions = missing_permissions(
                    alert_channel,
                    bot_member,
                    (
                        ("view_channel", "View Channel"),
                        ("send_messages", "Send Messages"),
                        ("embed_links", "Embed Links"),
                    ),
                )
                if missing_alert_permissions:
                    await interaction.followup.send(
                        "I cannot open a support room because I am missing permissions in "
                        "the configured alert channel: "
                        f"**{', '.join(missing_alert_permissions)}**.",
                        ephemeral=True,
                    )
                    return

                support_roles = valid_roles(guild, settings["support_role_ids"])
                overwrites: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
                    guild.default_role: discord.PermissionOverwrite(
                        view_channel=False, connect=False
                    ),
                    member: discord.PermissionOverwrite(
                        view_channel=True,
                        connect=True,
                        speak=True,
                        stream=True,
                        use_voice_activation=True,
                    ),
                    bot_member: discord.PermissionOverwrite(
                        view_channel=True,
                        connect=True,
                        move_members=True,
                        manage_channels=True,
                    ),
                }
                for role in support_roles:
                    overwrites[role] = discord.PermissionOverwrite(
                        view_channel=True,
                        connect=True,
                        speak=True,
                        stream=True,
                        use_voice_activation=True,
                        move_members=True,
                    )

                try:
                    room = await guild.create_voice_channel(
                        name=render_channel_name(
                            settings["channel_name_template"],
                            member,
                            max_length=MAX_CHANNEL_NAME_LENGTH,
                        ),
                        category=category,
                        overwrites=overwrites,
                        reason=f"Voice support requested by {member} ({member.id})",
                    )
                except discord.Forbidden:
                    log.warning(
                        "Forbidden creating VoiceAlert room: guild=%s requester=%s",
                        guild.id,
                        member.id,
                    )
                    await interaction.followup.send(
                        "I do not have permission to create the support room.", ephemeral=True
                    )
                    return

                except discord.HTTPException:
                    log.exception(
                        "Failed to create VoiceAlert room: guild=%s requester=%s",
                        guild.id,
                        member.id,
                    )
                    await interaction.followup.send(
                        "Discord could not create the support room. Please try again shortly.",
                        ephemeral=True,
                    )
                    return

                created_at = discord.utils.utcnow()
                room_record = {
                    "channel_id": room.id,
                    "created_at": int(created_at.timestamp()),
                    "has_been_occupied": False,
                }
                try:
                    async with self.cog.config.guild(guild).active_rooms() as rooms:
                        rooms[str(member.id)] = room_record
                except Exception:
                    log.exception(
                        "Failed to persist VoiceAlert room: guild=%s requester=%s "
                        "channel=%s",
                        guild.id,
                        member.id,
                        room.id,
                    )
                    await self._rollback_unpersisted_room(guild, member.id, room)
                    await interaction.followup.send(
                        "The room could not be saved, so it was not opened. Please try again.",
                        ephemeral=True,
                    )
                    return

                self._last_created_at[key] = now
                requester_moved = False

                if member.voice is not None and member.voice.channel is not None:
                    if room.permissions_for(bot_member).move_members:
                        try:
                            await member.move_to(
                                room, reason="Moved into requested voice support room"
                            )
                            requester_moved = True
                            room_record["has_been_occupied"] = True
                            await self._set_room_occupied(
                                guild, member.id, room.id, True
                            )
                        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                            log.warning(
                                "Could not move VoiceAlert requester: guild=%s "
                                "requester=%s channel=%s",
                                guild.id,
                                member.id,
                                room.id,
                                exc_info=True,
                            )

                if not requester_moved and not room.members:
                    await self._schedule_managed_cleanup(
                        guild, str(member.id), room, room_record
                    )

                try:
                    await self._send_room_response(interaction, room, created=True)
                finally:
                    try:
                        await self._send_alert(
                            alert_channel, member, room, created_at, settings
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        log.exception(
                            "Unexpected VoiceAlert alert failure: guild=%s requester=%s "
                            "channel=%s",
                            guild.id,
                            member.id,
                            room.id,
                        )
        finally:
            lock_entry.users -= 1
            if (
                lock_entry.users == 0
                and self._creation_locks.get(key) is lock_entry
            ):
                self._creation_locks.pop(key, None)

    async def _send_alert(
        self,
        channel: discord.TextChannel,
        requester: discord.Member,
        room: discord.VoiceChannel,
        created_at: datetime,
        settings: dict,
    ) -> None:
        """Notify configured roles that a support room was created."""
        ping_roles = valid_roles(channel.guild, settings["ping_role_ids"])
        content = " ".join(role.mention for role in ping_roles)
        embed = discord.Embed(
            title="Voice Support Requested",
            colour=discord.Colour.blurple(),
            timestamp=created_at,
        )
        embed.set_author(name=requester.display_name, icon_url=requester.display_avatar.url)
        embed.add_field(name="Requester", value=requester.mention, inline=True)
        embed.add_field(name="Room", value=room.mention, inline=True)
        embed.add_field(
            name="Created",
            value=discord.utils.format_dt(created_at, style="F"),
            inline=False,
        )
        embed.add_field(name="Requester ID", value=f"`{requester.id}`", inline=False)
        embed.set_footer(text=f"VoiceAlert • {channel.guild.name}")

        bot_member = channel.guild.me
        if bot_member is None:
            log.warning(
                "Cannot resolve bot member for VoiceAlert alert: guild=%s "
                "requester=%s channel=%s",
                channel.guild.id,
                requester.id,
                channel.id,
            )
            return
        permissions = channel.permissions_for(bot_member)
        if not permissions.view_channel or not permissions.send_messages:
            log.warning(
                "Missing permissions for VoiceAlert alert: guild=%s requester=%s "
                "channel=%s",
                channel.guild.id,
                requester.id,
                channel.id,
            )
            return
        try:
            await channel.send(
                content=content or None,
                embed=embed,
                allowed_mentions=discord.AllowedMentions(
                    everyone=False,
                    users=False,
                    roles=ping_roles,
                    replied_user=False,
                ),
            )
        except (discord.Forbidden, discord.NotFound):
            log.warning(
                "Cannot send VoiceAlert alert: guild=%s requester=%s channel=%s",
                channel.guild.id,
                requester.id,
                channel.id,
            )
        except discord.HTTPException:
            log.exception(
                "Discord rejected VoiceAlert alert: guild=%s requester=%s channel=%s",
                channel.guild.id,
                requester.id,
                channel.id,
            )

    async def _rollback_unpersisted_room(
        self,
        guild: discord.Guild,
        requester_id: int,
        room: discord.VoiceChannel,
    ) -> None:
        """Delete a room after persistence failure or track it for retries."""
        try:
            await room.delete(reason="VoiceAlert could not persist the room")
        except discord.NotFound:
            await self._remove_active_room_if_matches(
                guild, requester_id, room.id
            )
        except (discord.Forbidden, discord.HTTPException):
            log.exception(
                "Could not roll back unpersisted VoiceAlert room; scheduling retry: "
                "guild=%s requester=%s channel=%s",
                guild.id,
                requester_id,
                room.id,
            )
            self._orphan_rooms[room.id] = (guild.id, requester_id)
            await self._schedule_orphan_deletion(guild.id, requester_id, room.id)
        except Exception:
            log.exception(
                "Unexpected rollback failure; scheduling orphan recovery: "
                "guild=%s requester=%s channel=%s",
                guild.id,
                requester_id,
                room.id,
            )
            self._orphan_rooms[room.id] = (guild.id, requester_id)
            await self._schedule_orphan_deletion(guild.id, requester_id, room.id)
        else:
            await self._remove_active_room_if_matches(
                guild, requester_id, room.id
            )

    async def _schedule_orphan_deletion(
        self, guild_id: int, requester_id: int, channel_id: int
    ) -> None:
        """Ensure one retrying deletion task exists for an orphaned room."""
        if self.cog._unloading:
            log.error(
                "Orphan scheduling reached during VoiceAlert unload; attempting "
                "synchronous rollback: guild=%s requester=%s channel=%s",
                guild_id,
                requester_id,
                channel_id,
            )
            guild = self.cog.bot.get_guild(guild_id)
            if guild is None:
                log.error(
                    "Cannot synchronously roll back orphaned VoiceAlert room because "
                    "the guild is unavailable: "
                    "guild=%s requester=%s channel=%s",
                    guild_id,
                    requester_id,
                    channel_id,
                )
                return
            channel = guild.get_channel(channel_id)
            if channel is None:
                self._orphan_rooms.pop(channel_id, None)
                await self._remove_active_room_if_matches(
                    guild, requester_id, channel_id
                )
                return
            try:
                await channel.delete(
                    reason="VoiceAlert synchronous persistence rollback during unload"
                )
            except discord.NotFound:
                pass
            except (discord.Forbidden, discord.HTTPException):
                log.exception(
                    "Synchronous orphan rollback failed during VoiceAlert unload: "
                    "guild=%s requester=%s channel=%s",
                    guild_id,

                    requester_id,
                    channel_id,
                )
                return
            except Exception:
                log.exception(
                    "Unexpected synchronous orphan rollback failure during unload: "
                    "guild=%s requester=%s channel=%s",
                    guild_id,
                    requester_id,
                    channel_id,
                )
                return
            self._orphan_rooms.pop(channel_id, None)
            await self._remove_active_room_if_matches(
                guild, requester_id, channel_id
            )
            return

        existing = self._orphan_deletion_tasks.get(channel_id)
        if existing is not None and not existing.done():
            return
        self._orphan_deletion_tasks[channel_id] = asyncio.create_task(
            self._delete_orphan_with_retries(guild_id, requester_id, channel_id),
            name=f"voicealert-orphan-{channel_id}",
        )

    async def _delete_orphan_with_retries(
        self, guild_id: int, requester_id: int, channel_id: int
    ) -> None:
        """Retry deletion of a room that could not be persisted."""
        completed = False
        attempt = 0
        try:
            while True:
                attempt += 1
                await asyncio.sleep(
                    min(300, self.cog.ORPHAN_RETRY_SECONDS * attempt)
                )
                guild = self.cog.bot.get_guild(guild_id)
                if guild is None:
                    log.warning(
                        "Cannot recover orphaned VoiceAlert room because guild is "
                        "unavailable: guild=%s requester=%s channel=%s",
                        guild_id,
                        requester_id,
                        channel_id,
                    )
                    continue
                channel = guild.get_channel(channel_id)
                if channel is None:
                    await self._remove_active_room_if_matches(
                        guild, requester_id, channel_id
                    )
                    completed = True
                    return
                try:
                    await channel.delete(
                        reason="VoiceAlert retrying persistence rollback"
                    )
                except discord.NotFound:
                    pass
                except (discord.Forbidden, discord.HTTPException):
                    log.exception(
                        "Orphaned VoiceAlert room deletion retry failed: guild=%s "
                        "requester=%s channel=%s attempt=%s",
                        guild_id,
                        requester_id,
                        channel_id,
                        attempt,
                    )
                    continue
                await self._remove_active_room_if_matches(
                    guild, requester_id, channel_id
                )
                completed = True
                return
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception(
                "Unexpected orphan recovery failure: guild=%s requester=%s channel=%s",
                guild_id,
                requester_id,
                channel_id,
            )
        finally:
            current = self._orphan_deletion_tasks.get(channel_id)
            if current is asyncio.current_task():
                self._orphan_deletion_tasks.pop(channel_id, None)
            if completed:
                self._orphan_rooms.pop(channel_id, None)

    async def _cancel_deletion_task(self, channel_id: int) -> None:
        """Cancel and await one managed-room deletion task."""
        task = self._deletion_tasks.pop(channel_id, None)
        if task is None or task is asyncio.current_task():
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _schedule_managed_cleanup(
        self,
        guild: discord.Guild,
        requester_id: object,
        channel: discord.VoiceChannel,
        room_record: dict,
    ) -> None:
        """Schedule the correct initial or post-occupancy cleanup timeout."""
        if self.cog._unloading:
            log.warning(
                "Skipping managed VoiceAlert cleanup scheduling during unload: "
                "guild=%s requester=%s channel=%s",
                guild.id,
                requester_id,
                channel.id,
            )
            return
        if channel.members:
            return
        existing = self._deletion_tasks.get(channel.id)
        if existing is not None and not existing.done():
            return

        settings = await self.cog.config.guild(guild).all()
        occupied = bool(room_record.get("has_been_occupied", False))
        if occupied:
            delay = max(0, settings["empty_delete_delay_seconds"])
        else:
            created_at = room_record.get("created_at")
            try:
                created_timestamp = int(created_at)
            except (TypeError, ValueError):
                created_timestamp = int(discord.utils.utcnow().timestamp())
            deadline = created_timestamp + max(
                0, settings["initial_join_timeout_seconds"]
            )
            delay = max(0, deadline - discord.utils.utcnow().timestamp())

        requester_key = str(requester_id)
        self._deletion_tasks[channel.id] = asyncio.create_task(
            self._delete_room_when_empty(
                guild.id,
                requester_key,
                channel.id,
                occupied,
                delay,
            ),
            name=f"voicealert-delete-{channel.id}",
        )

    async def _handle_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        """Process a voice-state change for managed-room cleanup."""
        if before.channel == after.channel:
            return

        if isinstance(after.channel, discord.VoiceChannel):
            managed = await self._managed_room_record(member.guild, after.channel.id)
            if managed is not None:
                requester_id, _room_record = managed
                await self._set_room_occupied(
                    member.guild, requester_id, after.channel.id, True
                )
                await self._cancel_deletion_task(after.channel.id)

        if not isinstance(before.channel, discord.VoiceChannel) or before.channel.members:
            return
        managed = await self._managed_room_record(member.guild, before.channel.id)
        if managed is None:
            return
        requester_id, room_record = managed
        if not room_record.get("has_been_occupied", False):
            await self._set_room_occupied(
                member.guild, requester_id, before.channel.id, True
            )
            room_record["has_been_occupied"] = True
        await self._schedule_managed_cleanup(
            member.guild, requester_id, before.channel, room_record
        )

    async def _delete_room_when_empty(
        self,
        guild_id: int,
        requester_id: str,
        channel_id: int,
        expected_occupied: bool,
        delay: float,
    ) -> None:
        """Wait, re-check occupancy, then delete and forget an empty room."""
        try:
            while True:
                await asyncio.sleep(max(0, delay))
                guild = self.cog.bot.get_guild(guild_id)
                if guild is None:
                    return
                rooms = await self.cog.config.guild(guild).active_rooms()
                room_record = rooms.get(requester_id)
                if (
                    room_record is None
                    or room_record.get("channel_id") != channel_id
                ):
                    return

                channel = guild.get_channel(channel_id)
                if channel is None:
                    await self._remove_active_room_if_matches(
                        guild, requester_id, channel_id
                    )
                    return
                if not isinstance(channel, discord.VoiceChannel):
                    return
                if channel.members:
                    await self._set_room_occupied(
                        guild, requester_id, channel_id, True
                    )
                    return

                occupied = bool(room_record.get("has_been_occupied", False))
                if occupied != expected_occupied:
                    expected_occupied = occupied
                    if occupied:
                        delay = max(
                            0,
                            await self.cog.config.guild(
                                guild
                            ).empty_delete_delay_seconds(),
                        )
                    else:
                        initial_timeout = max(
                            0,
                            await self.cog.config.guild(
                                guild
                            ).initial_join_timeout_seconds(),
                        )
                        try:
                            created_at = int(room_record.get("created_at"))
                        except (TypeError, ValueError):
                            created_at = int(discord.utils.utcnow().timestamp())
                        delay = max(
                            0,
                            created_at
                            + initial_timeout
                            - discord.utils.utcnow().timestamp(),
                        )
                    continue

                try:
                    await channel.delete(
                        reason="VoiceAlert support room became empty"
                    )
                except discord.NotFound:
                    pass
                except (discord.Forbidden, discord.HTTPException):
                    log.exception(
                        "Failed to delete empty VoiceAlert room; retrying: "
                        "guild=%s requester=%s channel=%s",
                        guild_id,
                        requester_id,
                        channel_id,
                    )
                    delay = max(60, delay)
                    continue
                await self._remove_active_room_if_matches(
                    guild, requester_id, channel_id
                )
                return
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception(
                "Unexpected VoiceAlert room cleanup failure: guild=%s requester=%s "
                "channel=%s",
                guild_id,
                requester_id,
                channel_id,
            )
        finally:
            current = self._deletion_tasks.get(channel_id)
            if current is asyncio.current_task():
                self._deletion_tasks.pop(channel_id, None)

    async def _handle_guild_channel_delete(
        self, channel: discord.abc.GuildChannel
    ) -> None:
        """Update managed-room state for one channel-deletion event."""
        if not isinstance(channel, discord.VoiceChannel):
            return
        requester_id: Optional[str] = None
        try:
            managed = await self._managed_room_record(channel.guild, channel.id)
            await self._cancel_deletion_task(channel.id)
            if managed is not None:
                requester_id, _room_record = managed
                await self._remove_active_room_if_matches(
                    channel.guild, requester_id, channel.id
                )
            orphan_task = self._orphan_deletion_tasks.pop(channel.id, None)
            if orphan_task is not None and orphan_task is not asyncio.current_task():
                orphan_task.cancel()
                await asyncio.gather(orphan_task, return_exceptions=True)
            self._orphan_rooms.pop(channel.id, None)
        except Exception:
            log.exception(
                "Failed to remove stale VoiceAlert record: guild=%s requester=%s "
                "channel=%s",
                channel.guild.id,
                requester_id,
                channel.id,
            )

    async def _run_voicealert_cleanup(
        self, ctx: commands.Context, delete_empty: bool
    ) -> None:
        """Run cleanup while registered as an active operation."""
        guild_lock = self._guild_operation_locks.setdefault(
            ctx.guild.id, asyncio.Lock()
        )
        async with guild_lock:
            removed, deleted, failed = await self._cleanup_guild_rooms(
                ctx, delete_empty
            )

        await ctx.send(
            f"Cleanup complete: **{removed}** stale record(s) removed, "
            f"**{deleted}** empty room(s) deleted, **{failed}** deletion(s) failed."
        )

    async def _cleanup_guild_rooms(
        self, ctx: commands.Context, delete_empty: bool
    ) -> tuple[int, int, int]:
        """Clean recorded rooms while the caller holds the guild operation lock."""
        rooms = await self.cog.config.guild(ctx.guild).active_rooms()
        removed = 0
        deleted = 0
        failed = 0
        for requester_id, room_record in rooms.items():
            channel_id = room_record.get("channel_id")
            channel = ctx.guild.get_channel(channel_id)
            if not isinstance(channel, discord.VoiceChannel):
                removed += int(
                    await self._remove_active_room_if_matches(
                        ctx.guild, requester_id, channel_id
                    )
                )
                continue
            if not delete_empty or channel.members:
                continue
            try:
                await channel.delete(reason=f"VoiceAlert cleanup run by {ctx.author}")
            except discord.NotFound:
                pass
            except (discord.Forbidden, discord.HTTPException):
                failed += 1
                log.exception(
                    "Could not manually clean up VoiceAlert room: guild=%s "
                    "requester=%s channel=%s",
                    ctx.guild.id,
                    requester_id,
                    channel.id,
                )
                await self._schedule_managed_cleanup(
                    ctx.guild, requester_id, channel, room_record
                )
                continue
            await self._cancel_deletion_task(channel.id)
            removed += int(
                await self._remove_active_room_if_matches(
                    ctx.guild, requester_id, channel.id
                )
            )
            deleted += 1

        return removed, deleted, failed

    async def _run_voicealert_reset(
        self, ctx: commands.Context, confirmation: bool
    ) -> None:
        """Run reset while registered as an active operation."""
        if not confirmation:
            await ctx.send(
                "This deletes all managed rooms, then clears VoiceAlert settings.\n"
                f"Run `{ctx.clean_prefix}voicealert reset true` to confirm."
            )
            return

        guild_lock = self._guild_operation_locks.setdefault(
            ctx.guild.id, asyncio.Lock()
        )
        async with guild_lock:
            await self._reset_guild_configuration(ctx)

    async def _reset_guild_configuration(self, ctx: commands.Context) -> None:
        """Delete managed rooms and clear Config while requests are excluded."""
        guild_config = self.cog.config.guild(ctx.guild)
        settings = await guild_config.all()
        rooms = settings["active_rooms"]
        bot_member = ctx.guild.me
        if bot_member is None:
            await ctx.send("Reset aborted: I could not resolve my server member.")
            return

        missing_manage = []
        for room_record in rooms.values():
            channel = ctx.guild.get_channel(room_record.get("channel_id"))
            if (
                isinstance(channel, discord.VoiceChannel)
                and not channel.permissions_for(bot_member).manage_channels
            ):
                missing_manage.append(channel.mention)
        if missing_manage:
            await ctx.send(
                "Reset aborted. I lack **Manage Channels** for: "
                + ", ".join(missing_manage)
            )
            return

        previously_enabled = settings["enabled"]
        await guild_config.enabled.set(False)
        tasks = []
        for room_record in rooms.values():
            channel_id = room_record.get("channel_id")
            task = self._deletion_tasks.pop(channel_id, None)
            if task is not None:
                task.cancel()
                tasks.append(task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        failed_rooms: list[str] = []
        deleted = 0
        for requester_id, room_record in rooms.items():
            channel_id = room_record.get("channel_id")
            channel = ctx.guild.get_channel(channel_id)
            if not isinstance(channel, discord.VoiceChannel):
                await self._remove_active_room_if_matches(
                    ctx.guild, requester_id, channel_id
                )
                continue
            try:
                await channel.delete(reason=f"VoiceAlert reset run by {ctx.author}")
            except discord.NotFound:
                pass
            except (discord.Forbidden, discord.HTTPException):
                failed_rooms.append(channel.mention)
                log.exception(
                    "Could not delete VoiceAlert room during reset: guild=%s "
                    "requester=%s channel=%s",
                    ctx.guild.id,
                    requester_id,
                    channel.id,
                )
                continue
            await self._remove_active_room_if_matches(
                ctx.guild, requester_id, channel.id
            )
            deleted += 1

        remaining_rooms = await guild_config.active_rooms()
        if failed_rooms or remaining_rooms:
            await guild_config.enabled.set(previously_enabled)
            for requester_id, room_record in remaining_rooms.items():
                channel = ctx.guild.get_channel(room_record.get("channel_id"))
                if isinstance(channel, discord.VoiceChannel):
                    await self._schedule_managed_cleanup(
                        ctx.guild, requester_id, channel, room_record
                    )
            details = (
                ", ".join(failed_rooms)
                if failed_rooms
                else "new or concurrently managed rooms remain"
            )
            await ctx.send(
                f"Reset aborted after deleting **{deleted}** room(s). Configuration "
                f"was preserved because these rooms could not be cleared: {details}."
            )
            return

        await guild_config.clear()
        await guild_config.schema_version.set(self.cog.SCHEMA_VERSION)
        await ctx.send(
            f"VoiceAlert was reset after deleting **{deleted}** managed room(s)."
        )
