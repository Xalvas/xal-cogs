import re

import discord
from redbot.core import commands, Config


class WordFilter(commands.Cog):
    """Simple word filter with logging."""

    default_guild = {
        "words": [],
        "log_channel": None,
        "exempt_roles": [],
    }

    def __init__(self, bot):
        self.bot = bot

        self.config = Config.get_conf(
            self,
            identifier=987654321123456,
            force_registration=True,
        )

        self.config.register_guild(**self.default_guild)

    @commands.group()
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def filter(self, ctx):
        """Manage the word filter."""
        pass

    @filter.command(name="add")
    async def filter_add(self, ctx, *, word: str):
        """Add a filtered word or phrase."""

        word = word.lower().strip()

        async with self.config.guild(ctx.guild).words() as words:
            if word in words:
                await ctx.send("That filter already exists.")
                return

            words.append(word)

        await ctx.send(f"✅ Added filter: `{word}`")

    @filter.command(name="remove")
    async def filter_remove(self, ctx, *, word: str):
        """Remove a filtered word or phrase."""

        word = word.lower().strip()

        async with self.config.guild(ctx.guild).words() as words:
            if word not in words:
                await ctx.send("That filter doesn't exist.")
                return

            words.remove(word)

        await ctx.send(f"✅ Removed filter: `{word}`")

    @filter.command(name="list")
    async def filter_list(self, ctx):
        """List configured filters."""

        words = await self.config.guild(ctx.guild).words()

        if not words:
            await ctx.send("No filters configured.")
            return

        output = "\n".join(sorted(words))

        if len(output) > 1900:
            await ctx.send(f"There are {len(words)} filters configured.")
            return

        await ctx.send(
            f"**Configured Filters ({len(words)})**\n```{output}```"
        )

    @filter.command(name="clear")
    async def filter_clear(self, ctx):
        """Remove all filters."""

        await self.config.guild(ctx.guild).words.set([])

        await ctx.send("✅ All filters have been cleared.")

    @filter.command(name="logchannel")
    async def filter_logchannel(
        self,
        ctx,
        channel: discord.TextChannel,
    ):
        """Set the filter log channel."""

        await self.config.guild(ctx.guild).log_channel.set(channel.id)

        await ctx.send(
            f"✅ Filter logs will be sent to {channel.mention}"
        )

    @filter.command(name="exemptrole")
    async def filter_exemptrole(
        self,
        ctx,
        role: discord.Role,
    ):
        """Toggle role exemption."""

        async with self.config.guild(ctx.guild).exempt_roles() as roles:
            if role.id in roles:
                roles.remove(role.id)
                await ctx.send(
                    f"✅ Removed exemption from **{role.name}**"
                )
            else:
                roles.append(role.id)
                await ctx.send(
                    f"✅ Added exemption for **{role.name}**"
                )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):

        if not message.guild:
            return

        if message.author.bot:
            return

        exempt_roles = await self.config.guild(
            message.guild
        ).exempt_roles()

        if any(role.id in exempt_roles for role in message.author.roles):
            return

        filters = await self.config.guild(message.guild).words()

        if not filters:
            return

        content = message.content.lower()

        matched_filter = None

        for entry in filters:

            entry = entry.lower()

            if " " in entry:
                if entry in content:
                    matched_filter = entry
                    break
            else:
                pattern = rf"\b{re.escape(entry)}\b"

                if re.search(pattern, content):
                    matched_filter = entry
                    break

        if not matched_filter:
            return

        try:
            await message.delete()
        except discord.Forbidden:
            pass
        except discord.NotFound:
            pass

        log_channel_id = await self.config.guild(
            message.guild
        ).log_channel()

        if not log_channel_id:
            return

        log_channel = message.guild.get_channel(log_channel_id)

        if not log_channel:
            return

        embed = discord.Embed(
            title="🚨 Filter Triggered",
            colour=discord.Colour.red(),
            timestamp=discord.utils.utcnow(),
        )

        embed.add_field(
            name="Member",
            value=f"{message.author.mention}\n`{message.author.id}`",
            inline=False,
        )

        embed.add_field(
            name="Channel",
            value=message.channel.mention,
            inline=False,
        )

        embed.add_field(
            name="Matched Filter",
            value=f"`{matched_filter}`",
            inline=False,
        )

        embed.add_field(
            name="Message Content",
            value=message.content[:1024] or "*No content*",
            inline=False,
        )

        embed.set_footer(
            text=f"{message.guild.name}"
        )

        try:
            await log_channel.send(embed=embed)
        except discord.Forbidden:
            pass
