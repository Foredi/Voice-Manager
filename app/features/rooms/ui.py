from __future__ import annotations

import asyncio
import re
from typing import Optional

import discord
from discord import app_commands

from app.core.config import ERROR_WEBHOOK_URL, log
from app.core.discord_utils import audit, is_admin, post_webhook, respond
from app.core.voice import set_voice_channel_status
from app.features.voice_activity.presentation import format_duration

async def get_managed_room(interaction: discord.Interaction, *, require_owner: bool = True) -> Optional[discord.VoiceChannel]:
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await respond(interaction, "Lệnh này chỉ dùng trong server.")
        return None

    voice_state = interaction.user.voice
    channel = voice_state.channel if voice_state else None
    if not isinstance(channel, discord.VoiceChannel):
        await respond(interaction, "Bạn cần ở trong room voice của mình để sử dụng chức năng này.")
        return None

    room = interaction.client._managed_rooms.get(channel.id) or interaction.client.db.get_room(channel.id)
    if room is None:
        await respond(interaction, "Room này không được quản lý bởi bot.")
        return None

    if require_owner and room["owner_id"] != interaction.user.id and not is_admin(interaction.user):
        await respond(interaction, "Chỉ chủ phòng hoặc quản trị viên mới có thể sử dụng chức năng này.")
        return None
    return channel


class RenameRoomModal(discord.ui.Modal, title="Đổi tên room"):
    new_name = discord.ui.TextInput(label="Tên room mới", min_length=1, max_length=100, placeholder="Ví dụ: Chill Room")

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        channel = await get_managed_room(interaction)
        if channel is None:
            return
        try:
            new_name = str(self.new_name).strip()
            await channel.edit(name=new_name, reason=f"Room renamed by {interaction.user}")
            room = interaction.client._managed_rooms.get(channel.id) or interaction.client.db.get_room(channel.id)
            saved = bool(
                interaction.guild
                and room
                and room["owner_id"] == interaction.user.id
            )
            if saved:
                await asyncio.to_thread(
                    interaction.client.db.set_room_name_preference,
                    interaction.guild.id,
                    interaction.user.id,
                    new_name,
                )
                interaction.client._room_name_cache[(interaction.guild.id, interaction.user.id)] = new_name
            await audit(interaction.guild, interaction.user, f"đổi tên room thành {new_name}", channel)
            suffix = " Tên này sẽ được dùng cho các phòng bạn tạo sau này." if saved else ""
            await interaction.edit_original_response(content=f"Đã đổi tên room thành **{channel.name}**.{suffix}")
        except discord.Forbidden:
            await interaction.edit_original_response(content="Bot thiếu quyền **Manage Channels**.")
            if interaction.guild:
                await post_webhook(ERROR_WEBHOOK_URL, f"**VoiceMaster permission error**\nGuild `{interaction.guild.id}`: missing Manage Channels while renaming room `{channel.id}`.")
        except discord.HTTPException as exc:
            await interaction.edit_original_response(content=f"Không thể đổi tên room: {exc}")
        except Exception:
            log.exception("Could not save the new room name preference")
            await interaction.edit_original_response(content="Đã đổi tên kênh nhưng không lưu được tên mặc định cho lần sau.")


class LimitRoomModal(discord.ui.Modal, title="Giới hạn thành viên"):
    limit = discord.ui.TextInput(label="Số người tối đa (0 = không giới hạn)", min_length=1, max_length=2, placeholder="0")

    async def on_submit(self, interaction: discord.Interaction) -> None:
        channel = await get_managed_room(interaction)
        if channel is None:
            return
        raw = str(self.limit).strip()
        if not raw.isdigit() or not 0 <= int(raw) <= 99:
            await interaction.response.send_message("Hãy nhập số từ 0 đến 99.", ephemeral=True)
            return
        value = int(raw)
        try:
            await channel.edit(user_limit=value, reason=f"Room limit changed by {interaction.user}")
            label = "không giới hạn" if value == 0 else str(value)
            await interaction.response.send_message(f"Đã đặt giới hạn room: **{label}**.", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message("Bot thiếu quyền **Manage Channels**.", ephemeral=True)
            await post_webhook(ERROR_WEBHOOK_URL, f"**VoiceMaster permission error**\nGuild `{channel.guild.id}`: missing Manage Channels while changing room limit `{channel.id}`.")
        except discord.HTTPException as exc:
            await interaction.response.send_message(f"Không thể đổi giới hạn: {exc}", ephemeral=True)


class ChannelStatusModal(discord.ui.Modal, title="Đặt trạng thái kênh"):
    status_text = discord.ui.TextInput(
        label="Trạng thái sẽ hiện dưới tên kênh voice",
        min_length=1,
        max_length=500,
        placeholder="Ví dụ: cu cặc",
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        channel = await get_managed_room(interaction)
        if channel is None:
            return
        text = str(self.status_text).strip()
        if not text:
            await interaction.response.send_message("Trạng thái kênh không được để trống.", ephemeral=True)
            return
        try:
            await set_voice_channel_status(channel, text)
            await interaction.response.send_message(f"Đã đặt trạng thái cho {channel.mention}.", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message(
                "Bot thiếu quyền **Set Voice Channel Status**; nếu bot chưa ở trong kênh thì cần thêm **Manage Channels**.",
                ephemeral=True,
            )
            await post_webhook(ERROR_WEBHOOK_URL, f"**VoiceMaster permission error**\nGuild `{channel.guild.id}`: unable to set voice status for channel `{channel.id}`.")
        except discord.HTTPException as exc:
            await interaction.response.send_message(f"Không thể đặt trạng thái kênh: {exc}", ephemeral=True)


class TransferOwnerModal(discord.ui.Modal, title="Chuyển quyền chủ phòng"):
    member_id = discord.ui.TextInput(label="ID hoặc mention thành viên", placeholder="123456789012345678 hoặc @member", min_length=2, max_length=30)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        channel = await get_managed_room(interaction)
        if channel is None or interaction.guild is None:
            return
        raw = str(self.member_id).strip()
        match = re.search(r"\d{15,22}", raw)
        if not match:
            await interaction.response.send_message("Không đọc được ID thành viên. Hãy bật Developer Mode để sao chép ID.", ephemeral=True)
            return
        member = interaction.guild.get_member(int(match.group(0)))
        if member is None:
            try:
                member = await interaction.guild.fetch_member(int(match.group(0)))
            except (discord.NotFound, discord.HTTPException):
                member = None
        if member is None or member.bot:
            await interaction.response.send_message("Không tìm thấy thành viên hợp lệ trong server.", ephemeral=True)
            return
        interaction.client.db.set_room_owner(channel.id, member.id)
        if channel.id in interaction.client._managed_rooms:
            interaction.client._managed_rooms[channel.id]["owner_id"] = member.id
        await audit(interaction.guild, interaction.user, f"chuyển chủ room cho {member} ({member.id})", channel)
        await interaction.response.send_message(f"Đã chuyển quyền chủ phòng sang {member.mention}.", ephemeral=True)


class ConfirmDeleteView(discord.ui.View):
    def __init__(self, requester_id: int, channel_id: int):
        super().__init__(timeout=60)
        self.requester_id = requester_id
        self.channel_id = channel_id

    @discord.ui.button(label="Xác nhận xóa room", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("Chỉ người yêu cầu mới xác nhận được.", ephemeral=True)
            return
        channel = interaction.guild.get_channel(self.channel_id) if interaction.guild else None
        if not isinstance(channel, discord.VoiceChannel):
            interaction.client.db.delete_room(self.channel_id)
            interaction.client._managed_rooms.pop(self.channel_id, None)
            await interaction.response.send_message("Room không còn tồn tại. Đã dọn dữ liệu.", ephemeral=True)
            return
        # The confirmation message may be inside the voice channel being deleted.
        # Acknowledge before deletion and do not send a followup to the removed channel.
        await interaction.response.send_message(
            "Đã nhận yêu cầu xóa phòng. Đang xử lý…", ephemeral=True
        )
        try:
            await channel.delete(reason=f"Permanent room deleted by {interaction.user}")
            if interaction.guild:
                await audit(interaction.guild, interaction.user, "xóa room", channel)
            interaction.client.db.delete_room(self.channel_id)
            interaction.client._managed_rooms.pop(self.channel_id, None)
        except discord.NotFound:
            interaction.client.db.delete_room(self.channel_id)
            interaction.client._managed_rooms.pop(self.channel_id, None)
            log.info("Room %s was already deleted; removed its database row", self.channel_id)
        except discord.Forbidden:
            log.warning("Could not delete room %s: missing Manage Channels permission", self.channel_id)
            if interaction.guild:
                await post_webhook(ERROR_WEBHOOK_URL, f"**VoiceMaster permission error**\nGuild `{interaction.guild.id}`: missing Manage Channels while deleting room `{self.channel_id}`.")
        except discord.HTTPException as exc:
            log.exception("Could not delete room %s: %s", self.channel_id, exc)

    @discord.ui.button(label="Hủy", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("Chỉ người yêu cầu mới hủy được.", ephemeral=True)
            return
        await interaction.response.edit_message(content="Đã hủy thao tác xóa room.", view=None)


def build_room_embed(channel: discord.VoiceChannel, guild: discord.Guild, room: dict) -> discord.Embed:
    owner = guild.get_member(room["owner_id"])
    embed = discord.Embed(
        title="⚙️ Chào mừng đến kênh thoại tạm thời của bạn",
        description=(
            "Điều khiển phòng bằng các menu bên dưới. Chọn **Cài đặt kênh** để đổi tên hoặc giới hạn người; "
            "chọn **Quyền kênh** để khóa, mở khóa, ẩn hoặc hiện phòng; chọn **Trạng thái kênh** "
            "để đặt dòng trạng thái bên dưới tên kênh voice.\n\n"
            "Các thay đổi được áp dụng ngay lập tức. Tên do chủ phòng đặt sẽ được lưu và dùng cho phòng sau. "
            "Dùng **Làm mới** để cập nhật thông tin phòng trên bảng."
        ),
        color=discord.Color.blurple(),
    )
    embed.add_field(name="Kênh", value=channel.mention, inline=True)
    embed.add_field(name="Chủ phòng", value=owner.mention if owner else f"<@{room['owner_id']}>", inline=True)
    embed.add_field(name="Thành viên", value=str(len(channel.members)), inline=True)
    embed.add_field(name="Giới hạn", value=str(channel.user_limit or "Không giới hạn"), inline=True)
    embed.add_field(name="Cài đặt kênh", value="Đổi tên · Giới hạn người", inline=True)
    embed.add_field(name="Quyền kênh", value="Khóa/mở khóa · Ẩn/hiện", inline=True)
    embed.add_field(name="Trạng thái kênh", value="Đặt nội dung hiển thị dưới tên kênh voice", inline=True)
    embed.set_footer(text="VoiceMaster • /room panel")
    if guild.me and guild.me.display_avatar:
        embed.set_thumbnail(url=guild.me.display_avatar.url)
    return embed


class RoomControlView(discord.ui.View):
    """Interactive embed panel for the room the clicking member currently owns."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.select(
        placeholder="Cài đặt kênh",
        min_values=1,
        max_values=1,
        row=0,
        custom_id="permanent_vm:channel_settings",
        options=[
            discord.SelectOption(label="Đổi tên kênh", value="rename", emoji="✏️"),
            discord.SelectOption(label="Giới hạn thành viên", value="limit", emoji="👥"),
        ],
    )
    async def channel_settings(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        channel = await get_managed_room(interaction)
        if channel is None:
            return
        if select.values[0] == "rename":
            await interaction.response.send_modal(RenameRoomModal())
        else:
            await interaction.response.send_modal(LimitRoomModal())

    @discord.ui.select(
        placeholder="Quyền kênh",
        min_values=1,
        max_values=1,
        row=1,
        custom_id="permanent_vm:channel_permissions",
        options=[
            discord.SelectOption(label="Khóa phòng", value="lock", emoji="🔒"),
            discord.SelectOption(label="Mở khóa phòng", value="unlock", emoji="🔓"),
            discord.SelectOption(label="Ẩn phòng", value="hide", emoji="🙈"),
            discord.SelectOption(label="Hiện phòng", value="show", emoji="👁️"),
        ],
    )
    async def channel_permissions(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        channel = await get_managed_room(interaction)
        if channel is None or interaction.guild is None:
            return
        action = select.values[0]
        try:
            if action == "lock":
                await channel.set_permissions(interaction.guild.default_role, connect=False, reason="Room locked")
                message = "Đã khóa phòng."
            elif action == "unlock":
                await channel.set_permissions(interaction.guild.default_role, connect=None, reason="Room unlocked")
                message = "Đã mở khóa phòng."
            elif action == "hide":
                await channel.set_permissions(interaction.guild.default_role, view_channel=False, reason="Room hidden")
                message = "Đã ẩn phòng khỏi thành viên thường."
            else:
                await channel.set_permissions(interaction.guild.default_role, view_channel=None, reason="Room visible")
                message = "Đã hiện phòng."
            await audit(interaction.guild, interaction.user, f"{message.rstrip('.')}", channel)
            await interaction.response.send_message(message, ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message("Bot thiếu quyền **Manage Channels** hoặc **Manage Roles**.", ephemeral=True)
            await post_webhook(ERROR_WEBHOOK_URL, f"**VoiceMaster permission error**\nGuild `{interaction.guild.id}`: missing Manage Channels/Manage Roles for room permission update `{channel.id}`.")
        except discord.HTTPException as exc:
            await interaction.response.send_message(f"Không thể cập nhật quyền kênh: {exc}", ephemeral=True)

    @discord.ui.select(
        placeholder="Trạng thái kênh",
        min_values=1,
        max_values=1,
        row=2,
        # Keep the previous custom ID so existing room embeds remain clickable after restart.
        custom_id="permanent_vm:bot_status",
        options=[
            discord.SelectOption(label="Đặt trạng thái kênh", value="set", emoji="✏️"),
            discord.SelectOption(label="Xóa trạng thái kênh", value="clear", emoji="🧹"),
        ],
    )
    async def voice_channel_status(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        channel = await get_managed_room(interaction)
        if channel is None:
            return
        if select.values[0] == "clear":
            try:
                await set_voice_channel_status(channel, None)
                await interaction.response.send_message("Đã xóa trạng thái kênh voice.", ephemeral=True)
            except discord.Forbidden:
                await interaction.response.send_message(
                    "Bot thiếu quyền **Set Voice Channel Status**; nếu bot chưa ở trong kênh thì cần thêm **Manage Channels**.",
                    ephemeral=True,
                )
            except discord.HTTPException as exc:
                await interaction.response.send_message(f"Không thể xóa trạng thái kênh: {exc}", ephemeral=True)
            return
        await interaction.response.send_modal(ChannelStatusModal())

    @discord.ui.button(label="Làm mới", emoji="🔄", style=discord.ButtonStyle.primary, row=3, custom_id="permanent_vm:refresh")
    async def refresh_panel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        channel = await get_managed_room(interaction)
        if channel is None or interaction.guild is None:
            return
        room = interaction.client.db.get_room(channel.id)
        await interaction.response.edit_message(embed=build_room_embed(channel, interaction.guild, room), view=self)

    @discord.ui.button(label="Chuyển chủ", emoji="👑", style=discord.ButtonStyle.primary, custom_id="permanent_vm:transfer")
    async def transfer_owner(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        channel = await get_managed_room(interaction)
        if channel is None:
            return
        await interaction.response.send_modal(TransferOwnerModal())

    @discord.ui.button(label="Xóa room", emoji="🗑️", style=discord.ButtonStyle.danger, custom_id="permanent_vm:delete")
    async def delete_room(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        channel = await get_managed_room(interaction)
        if channel is None:
            return
        await interaction.response.send_message(
            f"Bạn có chắc muốn xóa **{channel.name}**? Thao tác này không thể hoàn tác.",
            view=ConfirmDeleteView(interaction.user.id, channel.id),
            ephemeral=True,
        )
