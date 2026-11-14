import discord
from discord import app_commands
from discord.ext import commands


class AdminCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="chat", description="Bật hoặc tắt chatbot trong server này (quản trị viên)")
    @app_commands.describe(enabled="Bật hoặc tắt chức năng trả lời khi mention bot")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def chat(self, interaction: discord.Interaction, enabled: bool):
        if interaction.guild_id is None:
            await interaction.response.send_message("Lệnh này chỉ dùng trong server.", ephemeral=True)
            return
        await self.bot.store.set_chat_enabled(interaction.guild_id, enabled)
        state = "đã bật" if enabled else "đã tắt"
        await interaction.response.send_message(
            f"Chatbot {state} trong server này.",
            ephemeral=True
        )

    @chat.error
    async def chat_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.MissingPermissions):
            text = "Bạn cần quyền Manage Server để dùng lệnh này."
        else:
            text = "Không thể cập nhật cấu hình chatbot."
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)

    @app_commands.command(name="about", description="Thông tin về chatbot")
    async def about(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="Discord AI Chatbot",
            description=(
                "Trợ lý AI riêng tiếng Việt, xưng em và gọi anh, sử dụng Gemini API "
                "và bộ nhớ dài hạn theo từng người dùng trong từng server.\n\n"
                "Mention bot để trò chuyện. Dùng `/memory`, `/forget`, `/reset` "
                "để quản lý dữ liệu của bạn."
            ),
            color=discord.Color.blurple()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
