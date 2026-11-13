from __future__ import annotations

import discord
from discord import app_commands

from app.features.polls.ui import CreatePollModal, TemplateDesignerView, template_preview


class PollGroup(app_commands.Group):
    def __init__(self):
        super().__init__(name="poll", description="Tạo và quản lý cuộc bình chọn")

    @app_commands.command(name="create", description="Tạo poll mới trong kênh hiện tại")
    async def create(self, interaction: discord.Interaction):
        if interaction.guild is None or interaction.channel is None:
            await interaction.response.send_message("Lệnh này chỉ dùng trong server.", ephemeral=True)
            return
        await interaction.response.send_modal(CreatePollModal())

    @app_commands.command(name="template_create", description="Tạo mẫu giao diện embed để dùng lại cho poll")
    async def template_create(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message("Lệnh này chỉ dùng trong server.", ephemeral=True)
            return
        view = TemplateDesignerView(interaction.user.id)
        await interaction.response.send_message(
            "🎨 **Bộ chỉnh sửa mẫu embed poll**\nDùng các nút bên dưới để chỉnh từng phần, xem trước rồi lưu mẫu.",
            embed=template_preview(view.data),
            view=view,
            ephemeral=True,
        )

    @app_commands.command(name="templates", description="Xem các mẫu embed poll đã lưu trong server")
    async def templates(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message("Lệnh này chỉ dùng trong server.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            templates = await interaction.client.list_poll_templates(interaction.guild.id)
        except Exception:
            await interaction.followup.send("Không tải được mẫu. Hãy kiểm tra migration Supabase.", ephemeral=True)
            return
        if not templates:
            await interaction.followup.send("Server chưa có mẫu embed. Tạo poll, chỉnh embed rồi bấm **Lưu mẫu**.", ephemeral=True)
            return
        lines = [f"🎨 **{item['name']}** — {item.get('embed_title') or 'không đặt tiêu đề'}" for item in templates]
        await interaction.followup.send("**Mẫu embed poll**\n" + "\n".join(lines), ephemeral=True)

    @app_commands.command(name="template_delete", description="Xóa một mẫu embed poll theo tên")
    @app_commands.describe(name="Tên mẫu cần xóa")
    async def template_delete(self, interaction: discord.Interaction, name: str):
        if interaction.guild is None:
            await interaction.response.send_message("Lệnh này chỉ dùng trong server.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            templates = await interaction.client.list_poll_templates(interaction.guild.id)
            template = next((item for item in templates if item["name"].casefold() == name.casefold()), None)
            if not template:
                await interaction.followup.send("Không tìm thấy mẫu có tên đó.", ephemeral=True)
                return
            is_admin = isinstance(interaction.user, discord.Member) and interaction.user.guild_permissions.manage_guild
            if int(template["owner_id"]) != interaction.user.id and not is_admin:
                await interaction.followup.send("Chỉ người tạo mẫu hoặc quản trị viên server mới có thể xóa mẫu này.", ephemeral=True)
                return
            await interaction.client.delete_poll_template(interaction.guild.id, template["id"])
            await interaction.followup.send(f"🗑️ Đã xóa mẫu **{template['name']}**.", ephemeral=True)
        except Exception:
            await interaction.followup.send("Không xóa được mẫu. Kiểm tra log bot và migration Supabase.", ephemeral=True)

    @app_commands.command(name="end", description="Kết thúc poll trong kênh hiện tại")
    async def end(self, interaction: discord.Interaction, poll_id: str | None = None):
        if interaction.guild is None or interaction.channel is None:
            await interaction.response.send_message("Lệnh này chỉ dùng trong server.", ephemeral=True)
            return
        bot = interaction.client
        if poll_id:
            poll = await bot.get_poll(poll_id)
        else:
            polls = await bot.list_active_polls(interaction.guild.id)
            polls.sort(key=lambda row: row.get("created_at", ""), reverse=True)
            poll = next((row for row in polls if int(row["channel_id"]) == interaction.channel.id and int(row["creator_id"]) == interaction.user.id), None)
            if poll is None:
                poll = next((row for row in polls if int(row["channel_id"]) == interaction.channel.id), None)
            if poll is None and len(polls) == 1:
                poll = polls[0]
        if not poll or poll["guild_id"] != interaction.guild.id:
            await interaction.response.send_message("Không tìm thấy poll đang mở. Truyền `poll_id` để chọn poll cụ thể.", ephemeral=True)
            return
        allowed = poll["creator_id"] == interaction.user.id or (
            isinstance(interaction.user, discord.Member) and interaction.user.guild_permissions.manage_guild
        )
        if not allowed:
            await interaction.response.send_message("Chỉ người tạo poll hoặc quản trị viên server mới được kết thúc.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await bot.finish_poll(poll["id"])
        await interaction.followup.send("Poll đã kết thúc và kết quả được cập nhật.", ephemeral=True)
