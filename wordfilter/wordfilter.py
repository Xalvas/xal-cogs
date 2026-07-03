import discord

from redbot.core import commands, Config


class WordFilter(commands.Cog):
    """Simple word filter with logging."""

    default_guild = {
        "words": [],
        "log_channel": None,
        "exempt_roles": []
    }

    def __init__(self, bot):
        self.bot = bot

        self.config = Config.get_conf(
            self,
            identifier=987654321123456,
            force_registration=True
        )

        self.config.register_guild(**self.default_guild)

    #
    # Commands
    #

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
                await ctx.send("That word is already filtered.")
                return

            words.append(word)

        await ctx.send(f"Added `{word}` to the filter.")

    @filter.command(name="remove")
    async def filter_remove(self, ctx, *, word: str):
        """Remove a filtered word or phrase."""

        word = word.lower().strip()

        async with self.config.guild(ctx.guild).words() as words:
            if word not in words:
                await ctx.send("That word is not currently filtered.")
                return

            words.remove(word)

        await ctx.send(f"Removed `{word}` from the filter.")

    @filter.command(name="list")
    async def filter_list(self, ctx):
        """List filtered words."""

        words = await self.config.guild(ctx.guild).words()

        if not words:
            await ctx.send("No filtered words configured.")
            return

        output = "\n".join(f"• {word}" for word in sorted(words))

        await ctx.send(
            f"**Filtered Words ({len(words)})**\n```{output}```"
        )

    @filter.command(name="logchannel")
    async def filter_logchannel(self, ctx, channel: discord.TextChannel):
        """Set the log channel."""

        await self.config.guild(ctx.guild).log_channel.set(channel.id)

        await ctx.send(f"Log channel set to {channel.mention}")

    @filter.command(name="exemptrole")
    async def filter_exemptrole(self, ctx, role: discord.Role):
        """Add/remove an exempt role."""

        async with self.config.guild(ctx.guild).exempt_roles() as roles:
            if role.id in roles:
                roles.remove(role.id)
                await ctx.send(
                    f"Removed exemption from **{role.name}**."
                )
            else:
                roles.append(role.id)
                await ctx.send(
                    f"Added exemption for **{role.name}**."
                )

    #
    # Listener
    #

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):

        if not message.guild:
            return

        if message.author.bot:
            return

        exemptions = await self.config.guild(
            message.guild
        ).exempt_roles()

        if any(role.id in exemptions for role in message.author.roles):
            return

        words = await self.config.guild(message.guild).words()

        if not words:
            return

        content = message.content.lower()

        matched_word = None

        for word in words:
            if word.lower() in content:
                matched_word = word
                break

        if not matched_word:
            return

        log_channel_id = await self.config.guild(
            message.guild
        ).log_channel()

        try:
            await message.delete()
        except discord.Forbidden:
            pass

        if not log_channel_id:
            return

        log_channel = message.guild.get_channel(log_channel_id)

        if not log_channel:
            return

        embed = discord.Embed(
            title="🚨 Filter Triggered",
            colour=discord.Colour.red()
        )

        embed.add_field(
            name="User",
            value=f"{message.author} ({message.author.id})",
            inline=False
        )

        embed.add_field(
            name="Channel",
            value=message.channel.mention,
            inline=False
        )

        embed.add_field(
            name="Matched Word",
            value=matched_word,
            inline=False
        )

        embed.add_field(
            name="Message",
            value=message.content[:1024],
            inline=False
        )

        embed.timestamp = discord.utils.utcnow()

        await log_channel.send(embed=embed)