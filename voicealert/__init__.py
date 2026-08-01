from .voicealert import VoiceAlert


async def setup(bot):
    await bot.add_cog(VoiceAlert(bot))
