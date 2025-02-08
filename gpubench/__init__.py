from redbot.core.bot import Red
from redbot.core.utils import get_end_user_data_statement

from .gpubench import GPUBench

async def setup(bot: Red):
    await bot.add_cog(GPUBench(bot))
