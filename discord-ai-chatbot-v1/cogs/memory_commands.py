import discord
from discord import app_commands
from discord.ext import commands
from config import settings


class MemoryCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="memory", description="Xem những ký ức bot đang lưu về bạn trong server này")
    async def memory(self, interaction: discord.Interaction):
        if interaction.guild_id is None:
            await interaction.response.send_message("Lệnh này chỉ dùng trong server.", ephemeral=True)
            return
        rows = await self.bot.store.get_memories(
            interaction.guild_id, interaction.user.id, 30
        )
        if not rows:
            await interaction.response.send_message(
                "Em chưa lưu ký ức dài hạn nào về anh trong server này.",
                ephemeral=True
            )
            return
        lines = [
            f"**{i}.** {row['content']}  ·  `{row['category']}`"
            for i, row in enumerate(rows, 1)
        ]
        embed = discord.Embed(
            title="Những điều em nhớ về anh",
            description="\n".join(lines)[:4000],
            color=discord.Color.blurple()
        )
        embed.set_footer(text="Ký ức được tách riêng theo từng server.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="forget", description="Xóa toàn bộ ký ức dài hạn của bạn trong server này")
    async def forget(self, interaction: discord.Interaction):
        if interaction.guild_id is None:
            await interaction.response.send_message("Lệnh này chỉ dùng trong server.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.bot.store.delete_memories(interaction.guild_id, interaction.user.id)
        await interaction.followup.send(
            "Đã xóa các ký ức dài hạn của bạn trong server này. Lịch sử chat vẫn còn; dùng /reset để xóa lịch sử ở kênh hiện tại.",
            ephemeral=True
        )

    @app_commands.command(name="reset", description="Xóa lịch sử trò chuyện của bạn với bot trong kênh hiện tại")
    async def reset(self, interaction: discord.Interaction):
        if interaction.guild_id is None or interaction.channel_id is None:
            await interaction.response.send_message("Lệnh này chỉ dùng trong server.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.bot.store.delete_history(
            interaction.guild_id, interaction.channel_id, interaction.user.id
        )
        await interaction.followup.send(
            "Đã xóa lịch sử trò chuyện của bạn với bot trong kênh hiện tại. Ký ức dài hạn không bị xóa.",
            ephemeral=True
        )
