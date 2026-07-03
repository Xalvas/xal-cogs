from .wordfilter import WordFilter


async def setup(bot):
    await bot.add_cog(WordFilter(bot))