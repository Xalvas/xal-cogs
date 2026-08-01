from __future__ import annotations

from typing import Optional

import discord


def valid_roles(guild: discord.Guild, role_ids: list[int]) -> list[discord.Role]:
    """Resolve configured roles, excluding deleted roles and @everyone."""
    roles: list[discord.Role] = []
    for role_id in role_ids:
        role = guild.get_role(role_id)
        if role is not None and not role.is_default():
            roles.append(role)
    return roles


def missing_permissions(
    channel: discord.abc.GuildChannel,
    member: discord.Member,
    required: tuple[tuple[str, str], ...],
) -> list[str]:
    """Return display names for permissions missing in a channel/category."""
    permissions = channel.permissions_for(member)
    return [
        label
        for attribute, label in required
        if not getattr(permissions, attribute)
    ]


def unpingable_roles(
    guild: discord.Guild,
    settings: dict,
    alert_channel: Optional[discord.TextChannel] = None,
) -> list[discord.Role]:
    """Return configured roles the bot cannot actually mention."""
    roles = valid_roles(guild, settings["ping_role_ids"])
    if not roles:
        return []
    channel = alert_channel
    if channel is None:
        resolved = guild.get_channel(settings["alert_channel_id"])
        channel = resolved if isinstance(resolved, discord.TextChannel) else None
    bot_member = guild.me
    can_mention_all = bool(
        channel is not None
        and bot_member is not None
        and channel.permissions_for(bot_member).mention_everyone
    )
    if can_mention_all:
        return []
    return [role for role in roles if not role.mentionable]


def channel_from_id(
    guild: discord.Guild,
    channel_id: Optional[int],
    expected_type: type[discord.abc.GuildChannel],
) -> Optional[discord.abc.GuildChannel]:
    """Resolve a guild channel only when it has the expected concrete type."""
    channel = guild.get_channel(channel_id) if channel_id else None
    return channel if isinstance(channel, expected_type) else None


def configuration_issues(guild: discord.Guild, settings: dict) -> list[str]:
    """Return human-readable descriptions of incomplete configuration."""
    issues: list[str] = []
    panel = channel_from_id(
        guild, settings["panel_channel_id"], discord.TextChannel
    )
    category = channel_from_id(
        guild, settings["category_id"], discord.CategoryChannel
    )
    alert = channel_from_id(
        guild, settings["alert_channel_id"], discord.TextChannel
    )
    if panel is None:
        issues.append("a valid panel text channel")
    if category is None:
        issues.append("a valid voice-room category")
    if alert is None:
        issues.append("a valid alert text channel")
    if not valid_roles(guild, settings["support_role_ids"]):
        issues.append("at least one valid support role")
    if not valid_roles(guild, settings["ping_role_ids"]):
        issues.append("at least one valid ping role")

    bot_member = guild.me
    if bot_member is None:
        issues.append("the bot's server member could not be resolved")
        return issues
    if isinstance(panel, discord.TextChannel):
        missing = missing_permissions(
            panel,
            bot_member,
            (
                ("view_channel", "View Channel"),
                ("send_messages", "Send Messages"),
                ("embed_links", "Embed Links"),
            ),
        )
        if missing:
            issues.append(f"panel permissions: {', '.join(missing)}")
    if isinstance(category, discord.CategoryChannel):
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
            issues.append(f"category permissions: {', '.join(missing)}")
    if isinstance(alert, discord.TextChannel):
        missing = missing_permissions(
            alert,
            bot_member,
            (
                ("view_channel", "View Channel"),
                ("send_messages", "Send Messages"),
                ("embed_links", "Embed Links"),
            ),
        )
        if missing:
            issues.append(f"alert permissions: {', '.join(missing)}")
        unpingable = unpingable_roles(guild, settings, alert)
        if unpingable:
            issues.append(
                "unmentionable ping roles: "
                + ", ".join(role.name for role in unpingable)
            )
    return issues
