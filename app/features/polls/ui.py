from __future__ import annotations

from datetime import datetime, timezone

import discord
from app.core.config import log


OPTION_EMOJIS = ["🌸", "✨", "💜", "🌙", "🪄", "🎀", "⭐", "🦋", "🍓", "🍀"]


def _remaining(ends_at: str | None) -> str:
    if not ends_at:
        return "Không giới hạn thời gian"
    end = datetime.fromisoformat(ends_at.replace("Z", "+00:00"))
    seconds = max(0, int((end - datetime.now(timezone.utc)).total_seconds()))
    if seconds >= 86400:
        return f"còn {seconds // 86400} ngày"
    if seconds >= 3600:
        return f"còn {seconds // 3600} giờ {seconds % 3600 // 60} phút"
    if seconds >= 60:
        return f"còn {seconds // 60} phút"
    return f"còn {seconds} giây"


def poll_embed(poll: dict, counts: dict[int, int], creator: str | None = None) -> discord.Embed:
    options = poll["options"]
    total = sum(counts.values())
    ended = poll["ended"]
    embed_title = poll.get("embed_title") or poll["question"]
    description = []
    if poll.get("embed_title"):
        # Discord supports markdown headings inside embed descriptions; use a
        # level-two heading so the actual poll question stands out from template copy.
        description.append(f"## {poll['question']}")
    if poll.get("description"):
        description.append(poll["description"])
    description.append("🔒 **Cuộc bình chọn đã khép lại**" if ended else "🗳️ Hãy chọn đáp án bạn yêu thích nhé!")
    embed = discord.Embed(
        title=f"✨ {embed_title}",
        description="\n\n".join(description),
        color=int(poll.get("embed_color") or 0xD8A7FF) if not ended else int(poll.get("embed_color") or 0x9B7BB8),
    )
    peak = max((counts.get(int(option["id"]), 0) for option in options), default=0)
    for index, option in enumerate(options):
        votes = counts.get(int(option["id"]), 0)
        pct = round(votes * 100 / total) if total else 0
        marker = "  🏆" if ended and peak > 0 and votes == peak else ""
        emoji = f"{option.get('emoji') or OPTION_EMOJIS[index % len(OPTION_EMOJIS)]}  "
        if total or ended:
            filled = round(pct / 10)
            bar = "▰" * filled + "▱" * (10 - filled)
            value = f"`{bar}`  **{pct}%**  ·  {votes} phiếu{marker}"
        else:
            value = "🌱 Chưa có phiếu  ·  **0%**"
        embed.add_field(name=f"{emoji}{option['label']}", value=value, inline=False)
    if ended and peak:
        winners = [o["label"] for o in options if counts.get(int(o["id"]), 0) == peak]
        embed.add_field(name="Kết quả", value="Đồng hạng: " + ", ".join(winners) if len(winners) > 1 else f"Đáp án dẫn đầu: **{winners[0]}**", inline=False)
    author_name = poll.get("author_name") or creator
    author_icon = poll.get("author_icon_url")
    if author_name:
        embed.set_author(name=author_name, icon_url=author_icon)
    mode = "Chọn một" if poll["single_choice"] else "Chọn nhiều"
    timing = "Đã kết thúc" if ended else _remaining(poll.get("ends_at"))
    status = f"🗳️ {total} phiếu   ·   ⏳ {timing}   ·   {mode}"
    footer = f"{poll['footer_text']}\n{status}" if poll.get("footer_text") else status
    embed.set_footer(text=footer, icon_url=poll.get("footer_icon_url"))
    if poll.get("thumbnail_url"):
        embed.set_thumbnail(url=poll["thumbnail_url"])
    if poll.get("image_url"):
        embed.set_image(url=poll["image_url"])
    return embed


class PollVoteView(discord.ui.View):
    def __init__(self, poll: dict):
        super().__init__(timeout=None)
        self.poll = poll
        self.poll_id = poll["id"]
        self.ended = poll["ended"]
        options = poll["options"]
        select = discord.ui.Select(
            custom_id="poll:vote",
            placeholder="Chọn một đáp án" if poll["single_choice"] else "Chọn một hoặc nhiều đáp án",
            min_values=1,
            max_values=1 if poll["single_choice"] else len(options),
            options=[discord.SelectOption(label=item["label"][:100], value=str(item["id"]), emoji=item.get("emoji") or None) for item in options],
            disabled=self.ended,
            row=0,
        )
        select.callback = self._select_callback
        self.add_item(select)

    async def _select_callback(self, interaction: discord.Interaction):
        # Ack immediately. Apply the visible result from the warm in-memory cache;
        # the Supabase write runs after Discord has been edited.
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            poll = self.poll
            if poll["ended"] or (poll.get("ends_at") and datetime.fromisoformat(poll["ends_at"].replace("Z", "+00:00")) <= datetime.now(timezone.utc)):
                if not poll["ended"]:
                    await interaction.client.finish_poll(poll["id"])
                await interaction.followup.send("Poll này đã kết thúc.", ephemeral=True)
                return
            selected = [int(value) for value in interaction.data.get("values", [])]
            await interaction.client.optimistic_poll_vote(poll["id"], interaction.user.id, selected)
            message_updated = False
            try:
                message_updated = await interaction.client.update_poll_message_fast(interaction.message, poll)
            except discord.HTTPException:
                log.exception("Could not update poll message immediately for poll %s", poll["id"])
            interaction.client.queue_poll_vote_persist(poll["id"], interaction.user.id)
            status = "✅ Kết quả Discord đã cập nhật ngay; phiếu đang được lưu." if message_updated else "✅ Phiếu đang được lưu; Discord chưa cập nhật được kết quả hiển thị."
            await interaction.followup.send(status, ephemeral=True)
        except Exception:
            from app.core.config import log
            log.exception("Could not apply poll vote immediately")
            await interaction.followup.send("Không lưu được phiếu. Hãy thử lại sau.", ephemeral=True)


class CreatePollModal(discord.ui.Modal, title="Tạo poll"):
    question = discord.ui.TextInput(label="Câu hỏi", max_length=250, placeholder="Bạn muốn hỏi điều gì?", required=True)
    options = discord.ui.TextInput(label="Đáp án (mỗi dòng một đáp án)", style=discord.TextStyle.paragraph, max_length=900, placeholder="Có\nKhông\nCó thể", required=True)
    duration = discord.ui.TextInput(label="Thời lượng phút (0 = không giới hạn)", default="1440", max_length=6, required=True)
    mode = discord.ui.TextInput(label="Kiểu vote: single hoặc multi", default="single", max_length=6, required=True)

    async def on_submit(self, interaction: discord.Interaction):
        labels = [line.strip() for line in str(self.options).splitlines() if line.strip()]
        if not 2 <= len(labels) <= 10:
            await interaction.response.send_message("Poll cần từ 2 đến 10 đáp án (mỗi dòng một đáp án).", ephemeral=True)
            return
        try:
            minutes = int(str(self.duration).strip())
            if minutes < 0 or minutes > 525600:
                raise ValueError
        except ValueError:
            await interaction.response.send_message("Thời lượng phải là số phút từ 0 đến 525600.", ephemeral=True)
            return
        mode = str(self.mode).strip().lower()
        if mode not in {"single", "multi"}:
            await interaction.response.send_message("Kiểu vote phải là `single` hoặc `multi`.", ephemeral=True)
            return
        emojis = [OPTION_EMOJIS[i % len(OPTION_EMOJIS)] for i in range(len(labels))]
        draft = {
            "question": str(self.question), "labels": labels, "emojis": emojis,
            "single_choice": mode == "single", "minutes": minutes,
            "embed_title": None, "description": None, "embed_color": 0xD8A7FF,
            "thumbnail_url": None, "image_url": None,
            "author_name": None, "author_icon_url": None,
            "footer_text": None, "footer_icon_url": None,
        }
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            templates = await interaction.client.list_poll_templates(interaction.guild_id)
            if templates:
                apply_template(draft, templates[0])
            view = PollDraftView(draft, interaction.user.id, templates)
            await interaction.followup.send(
                "🌷 **Bản nháp poll của bạn đã sẵn sàng!**\n"
                + ("Mẫu embed gần nhất đã được áp dụng. Bạn có thể đổi mẫu hoặc chỉnh tiếp trước khi đăng." if templates else "Trang trí embed rồi lưu thành mẫu để dùng lại ở poll sau."),
                embed=draft_embed(draft, interaction.user.display_name),
                view=view,
                ephemeral=True,
            )
        except Exception:
            await interaction.followup.send("Không tải được mẫu embed. Kiểm tra migration Supabase rồi thử lại.", ephemeral=True)


def draft_embed(draft: dict, creator: str) -> discord.Embed:
    poll = {
        **draft,
        "ended": False,
        "single_choice": draft["single_choice"],
        "ends_at": None,
        "options": [{"id": index, "label": label, "emoji": draft["emojis"][index]} for index, label in enumerate(draft["labels"])],
    }
    embed = poll_embed(poll, {}, creator)
    embed.set_footer(text="✨ Bản xem trước · Chỉnh giao diện hoặc đăng poll")
    return embed


def apply_template(draft: dict, template: dict) -> None:
    draft.update({
        "template_id": template["id"],
        "template_name": template["name"],
        "embed_title": template.get("embed_title"),
        "description": template.get("description"),
        "embed_color": int(template.get("embed_color") or 0xD8A7FF),
        "thumbnail_url": template.get("thumbnail_url"),
        "image_url": template.get("image_url"),
        "author_name": template.get("author_name"),
        "author_icon_url": template.get("author_icon_url"),
        "footer_text": template.get("footer_text"),
        "footer_icon_url": template.get("footer_icon_url"),
    })


class PollDraftView(discord.ui.View):
    def __init__(self, draft: dict, owner_id: int, templates: list[dict] | None = None):
        super().__init__(timeout=600)
        self.draft = draft
        self.owner_id = owner_id
        self.templates = templates or []
        if self.templates:
            select = discord.ui.Select(
                placeholder=f"Mẫu đang dùng: {draft.get('template_name', 'Chọn mẫu embed')}",
                min_values=1,
                max_values=1,
                options=[
                    discord.SelectOption(
                        label=template["name"][:100],
                        value=str(template["id"]),
                        description=(template.get("embed_title") or "Mẫu embed poll")[:100],
                        default=str(template["id"]) == str(draft.get("template_id")),
                    )
                    for template in self.templates[:25]
                ],
                row=0,
            )
            select.callback = self.select_template
            self.add_item(select)

    async def select_template(self, interaction: discord.Interaction):
        selected_id = interaction.data["values"][0]
        template = next((item for item in self.templates if str(item["id"]) == selected_id), None)
        if template:
            apply_template(self.draft, template)
        await interaction.response.edit_message(embed=draft_embed(self.draft, interaction.user.display_name), view=self)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Chỉ người tạo bản nháp mới chỉnh sửa được.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="✏️ Nội dung", style=discord.ButtonStyle.secondary, row=1)
    async def edit_content(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(PollBasicModal(self.draft))

    @discord.ui.button(label="👤 Author", style=discord.ButtonStyle.secondary, row=1)
    async def edit_author(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AuthorModal(self.draft))

    @discord.ui.button(label="📌 Footer", style=discord.ButtonStyle.secondary, row=1)
    async def edit_footer(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(FooterModal(self.draft))

    @discord.ui.button(label="🖼️ Ảnh", style=discord.ButtonStyle.secondary, row=1)
    async def edit_images(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ImagesModal(self.draft))

    @discord.ui.button(label="😀 Emoji đáp án", style=discord.ButtonStyle.secondary, row=1)
    async def edit_option_emojis(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild is None:
            await interaction.response.send_message("Không lấy được emoji của server.", ephemeral=True)
            return
        await interaction.response.send_message(
            "Bấm vào đáp án cần chỉnh. Sau đó chọn emoji; emoji sẽ được áp dụng ngay.",
            view=PollEmojiAnswerView(self.draft, self, interaction.message, interaction.guild.emojis),
            ephemeral=True,
        )

    @discord.ui.button(label="👀 Xem trước", style=discord.ButtonStyle.secondary, row=2)
    async def preview(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=draft_embed(self.draft, interaction.user.display_name), view=self)

    @discord.ui.button(label="💾 Lưu mẫu", style=discord.ButtonStyle.secondary, row=2)
    async def save_template(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(PollTemplateNameModal(self.draft, self))

    @discord.ui.button(label="🚀 Đăng poll", style=discord.ButtonStyle.success, row=2)
    async def publish(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            bot = interaction.client
            poll_id = await bot.create_poll_record(
                interaction, self.draft["question"], self.draft["labels"], self.draft["emojis"],
                self.draft["single_choice"], self.draft["minutes"],
                embed_title=self.draft["embed_title"], description=self.draft["description"],
                embed_color=self.draft["embed_color"], thumbnail_url=self.draft["thumbnail_url"],
                image_url=self.draft["image_url"], author_name=self.draft.get("author_name"),
                author_icon_url=self.draft.get("author_icon_url"), footer_text=self.draft.get("footer_text"),
                footer_icon_url=self.draft.get("footer_icon_url"),
            )
            poll = await bot.get_poll(poll_id)
            # Warm the vote/count cache before exposing the interactive message.
            await bot.prime_poll_cache(poll)
            message = await interaction.channel.send(embed=poll_embed(poll, {}, interaction.user.display_name), view=PollVoteView(poll))
            await bot.save_poll_message(poll_id, message.id)
            poll["message_id"] = message.id
            await interaction.followup.send(f"🎉 Poll đã được đăng: {message.jump_url}", ephemeral=True)
            self.stop()
        except (discord.Forbidden, discord.HTTPException):
            await interaction.followup.send("Bot không thể đăng poll. Hãy kiểm tra quyền Send Messages và Embed Links.", ephemeral=True)
        except Exception as exc:
            from app.core.config import log
            log.exception("Poll publish failed for guild %s", interaction.guild_id)
            message = str(exc).lower()
            if "embed_title" in message or "embed_color" in message or "thumbnail_url" in message or "image_url" in message or "schema cache" in message:
                response = "Database poll chưa có các cột giao diện mới. Hãy chạy lại warehouse_star_schema.sql trên Supabase rồi thử tạo poll mới."
            else:
                response = "Không thể lưu poll. Lỗi đã được ghi vào log bot để xác định nguyên nhân."
            await interaction.followup.send(response, ephemeral=True)


class PollEmojiAnswerView(discord.ui.View):
    """Keeps answer selection as buttons so it cannot be cleared by a select refresh."""

    def __init__(self, draft: dict, parent_view: PollDraftView, parent_message, emojis):
        super().__init__(timeout=300)
        self.draft = draft
        self.parent_view = parent_view
        self.parent_message = parent_message
        self.emojis = list(emojis)
        for index, label in enumerate(draft["labels"]):
            button = discord.ui.Button(
                label=label[:75],
                emoji=draft["emojis"][index],
                style=discord.ButtonStyle.secondary,
                row=index // 5,
            )

            async def choose(interaction: discord.Interaction, answer_index=index):
                await interaction.response.edit_message(
                    content=f"Chọn emoji server cho **{self.draft['labels'][answer_index]}**:",
                    view=PollServerEmojiView(self, answer_index),
                )

            button.callback = choose
            self.add_item(button)


class PollServerEmojiView(discord.ui.View):
    PAGE_SIZE = 24

    def __init__(self, parent: PollEmojiAnswerView, answer_index: int):
        super().__init__(timeout=300)
        self.parent = parent
        self.answer_index = answer_index
        self.page = 0
        self.emoji_select = self._make_emoji_select()
        self.add_item(self.emoji_select)
        previous = discord.ui.Button(label="◀", style=discord.ButtonStyle.secondary, row=1, disabled=True)
        previous.callback = self._previous_page
        self.add_item(previous)
        next_button = discord.ui.Button(label="▶", style=discord.ButtonStyle.secondary, row=1, disabled=len(parent.emojis) <= self.PAGE_SIZE)
        next_button.callback = self._next_page
        self.add_item(next_button)
        back = discord.ui.Button(label="↩ Chọn đáp án khác", style=discord.ButtonStyle.secondary, row=1)
        back.callback = self._back
        self.add_item(back)

    def _make_emoji_select(self):
        start = self.page * self.PAGE_SIZE
        options = [discord.SelectOption(
            label="Emoji mặc định", value="__default__",
            emoji=OPTION_EMOJIS[self.answer_index % len(OPTION_EMOJIS)],
        )]
        options.extend(
            discord.SelectOption(label=emoji.name[:100], value=str(emoji.id), emoji=emoji)
            for emoji in self.parent.emojis[start:start + self.PAGE_SIZE]
        )
        select = discord.ui.Select(
            placeholder=f"Emoji của server · trang {self.page + 1}",
            options=options,
            row=0,
        )
        select.callback = self._choose_emoji
        return select

    async def _choose_emoji(self, interaction: discord.Interaction):
        chosen_id = interaction.data["values"][0]
        if chosen_id == "__default__":
            emoji_text = OPTION_EMOJIS[self.answer_index % len(OPTION_EMOJIS)]
        else:
            chosen = discord.utils.get(self.parent.emojis, id=int(chosen_id))
            if chosen is None:
                await interaction.response.send_message("Emoji không còn trong server.", ephemeral=True)
                return
            emoji_text = str(chosen)
        self.parent.draft["emojis"][self.answer_index] = emoji_text
        # Acknowledge first, then refresh both the draft preview and the same private picker.
        await interaction.response.defer()
        try:
            await self.parent.parent_message.edit(
                embed=draft_embed(self.parent.draft, interaction.user.display_name),
                view=self.parent.parent_view,
            )
        except discord.HTTPException:
            pass
        await interaction.edit_original_response(
            content=f"✅ Đã gán {emoji_text} cho **{self.parent.draft['labels'][self.answer_index]}**. Chọn tiếp đáp án khác nếu cần.",
            view=PollEmojiAnswerView(self.parent.draft, self.parent.parent_view, self.parent.parent_message, self.parent.emojis),
        )

    async def _previous_page(self, interaction: discord.Interaction):
        self.page = max(0, self.page - 1)
        self._refresh_page()
        await interaction.response.edit_message(view=self)

    async def _next_page(self, interaction: discord.Interaction):
        last_page = max(0, (len(self.parent.emojis) - 1) // self.PAGE_SIZE)
        self.page = min(last_page, self.page + 1)
        self._refresh_page()
        await interaction.response.edit_message(view=self)

    def _refresh_page(self):
        self.remove_item(self.emoji_select)
        self.emoji_select = self._make_emoji_select()
        self.add_item(self.emoji_select)
        for item in self.children:
            if isinstance(item, discord.ui.Button) and item.label == "◀":
                item.disabled = self.page == 0
            elif isinstance(item, discord.ui.Button) and item.label == "▶":
                item.disabled = (self.page + 1) * self.PAGE_SIZE >= len(self.parent.emojis)

    async def _back(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            content="Bấm vào đáp án cần chỉnh. Sau đó chọn emoji; emoji sẽ được áp dụng ngay.",
            view=PollEmojiAnswerView(self.parent.draft, self.parent.parent_view, self.parent.parent_message, self.parent.emojis),
        )


class PollTemplateNameModal(discord.ui.Modal, title="💾 Lưu mẫu embed"):
    name = discord.ui.TextInput(label="Tên mẫu", max_length=60, placeholder="VD: Thông báo sự kiện tím", required=True)

    def __init__(self, draft: dict, view: PollDraftView):
        super().__init__()
        self.draft = draft
        self.draft_view = view

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        template = {
            "guild_id": interaction.guild_id,
            "owner_id": interaction.user.id,
            "name": str(self.name).strip(),
            "embed_title": self.draft.get("embed_title"),
            "description": self.draft.get("description"),
            "embed_color": self.draft.get("embed_color", 0xD8A7FF),
            "thumbnail_url": self.draft.get("thumbnail_url"),
            "image_url": self.draft.get("image_url"),
            "author_name": self.draft.get("author_name"),
            "author_icon_url": self.draft.get("author_icon_url"),
            "footer_text": self.draft.get("footer_text"),
            "footer_icon_url": self.draft.get("footer_icon_url"),
        }
        try:
            saved = await interaction.client.save_poll_template(template)
            self.draft["template_id"] = saved.get("id")
            self.draft["template_name"] = template["name"]
            self.draft_view.templates = await interaction.client.list_poll_templates(interaction.guild_id)
            await interaction.followup.send(f"💾 Đã lưu mẫu **{template['name']}**. Mẫu sẽ được áp dụng tự động khi tạo poll tiếp theo.", ephemeral=True)
        except Exception:
            from app.core.config import log
            log.exception("Poll template save failed for guild %s", interaction.guild_id)
            await interaction.followup.send("Không lưu được mẫu embed. Hãy kiểm tra migration Supabase và đảm bảo tên mẫu chưa bị trùng.", ephemeral=True)


def _validate_color(value: str) -> int | None:
    color = value.strip().lstrip("#")
    if len(color) != 6 or any(char not in "0123456789abcdefABCDEF" for char in color):
        return None
    return int(color, 16)


def _valid_image_url(value: str) -> bool:
    return not value or value.startswith("https://")


class PollBasicModal(discord.ui.Modal, title="✏️ Nội dung embed"):
    title_text = discord.ui.TextInput(label="Tiêu đề", max_length=100, required=False)
    description_text = discord.ui.TextInput(label="Mô tả", style=discord.TextStyle.paragraph, max_length=700, required=False)
    color_text = discord.ui.TextInput(label="Màu HEX", max_length=7, default="#D8A7FF", required=True)

    def __init__(self, draft: dict):
        super().__init__()
        self.draft = draft
        self.title_text.default = draft.get("embed_title") or ""
        self.description_text.default = draft.get("description") or ""
        self.color_text.default = f"#{int(draft.get('embed_color') or 0xD8A7FF):06X}"

    async def on_submit(self, interaction: discord.Interaction):
        color = _validate_color(str(self.color_text))
        if color is None:
            await interaction.response.send_message("Màu cần có dạng `#D8A7FF`.", ephemeral=True)
            return
        self.draft.update({"embed_title": str(self.title_text).strip() or None,
                           "description": str(self.description_text).strip() or None,
                           "embed_color": color})
        await interaction.response.send_message("✅ Nội dung đã cập nhật. Bấm **Xem trước** để kiểm tra.", ephemeral=True)


class AuthorModal(discord.ui.Modal, title="👤 Chỉnh author"):
    name = discord.ui.TextInput(label="Tên author", max_length=100, required=False)
    icon_url = discord.ui.TextInput(label="Avatar URL (https://)", max_length=300, required=False)

    def __init__(self, data: dict):
        super().__init__()
        self.data = data
        self.name.default = data.get("author_name") or ""
        self.icon_url.default = data.get("author_icon_url") or ""

    async def on_submit(self, interaction: discord.Interaction):
        name, icon = str(self.name).strip() or None, str(self.icon_url).strip() or None
        if icon and not _valid_image_url(icon):
            await interaction.response.send_message("Avatar cần là URL `https://`.", ephemeral=True)
            return
        self.data.update({"author_name": name, "author_icon_url": icon})
        await interaction.response.send_message("✅ Author đã cập nhật. Bấm **Xem trước** để kiểm tra.", ephemeral=True)


class FooterModal(discord.ui.Modal, title="📌 Chỉnh footer"):
    text = discord.ui.TextInput(label="Nội dung footer", max_length=300, required=False)
    icon_url = discord.ui.TextInput(label="Icon URL (https://)", max_length=300, required=False)

    def __init__(self, data: dict):
        super().__init__()
        self.data = data
        self.text.default = data.get("footer_text") or ""
        self.icon_url.default = data.get("footer_icon_url") or ""

    async def on_submit(self, interaction: discord.Interaction):
        text, icon = str(self.text).strip() or None, str(self.icon_url).strip() or None
        if icon and not _valid_image_url(icon):
            await interaction.response.send_message("Icon cần là URL `https://`.", ephemeral=True)
            return
        self.data.update({"footer_text": text, "footer_icon_url": icon})
        await interaction.response.send_message("✅ Footer đã cập nhật. Bấm **Xem trước** để kiểm tra.", ephemeral=True)


class ImagesModal(discord.ui.Modal, title="🖼️ Chỉnh hình ảnh"):
    thumbnail = discord.ui.TextInput(label="Thumbnail URL (https://)", max_length=300, required=False)
    banner = discord.ui.TextInput(label="Banner URL (https://)", max_length=300, required=False)

    def __init__(self, data: dict):
        super().__init__()
        self.data = data
        self.thumbnail.default = data.get("thumbnail_url") or ""
        self.banner.default = data.get("image_url") or ""

    async def on_submit(self, interaction: discord.Interaction):
        thumbnail, banner = str(self.thumbnail).strip() or None, str(self.banner).strip() or None
        if not _valid_image_url(thumbnail or "") or not _valid_image_url(banner or ""):
            await interaction.response.send_message("Ảnh cần là URL `https://`.", ephemeral=True)
            return
        self.data.update({"thumbnail_url": thumbnail, "image_url": banner})
        await interaction.response.send_message("✅ Hình ảnh đã cập nhật. Bấm **Xem trước** để kiểm tra.", ephemeral=True)


class TemplateBasicModal(discord.ui.Modal, title="✏️ Nội dung mẫu"):
    name = discord.ui.TextInput(label="Tên mẫu", max_length=60, required=True)
    title_text = discord.ui.TextInput(label="Tiêu đề embed", max_length=100, required=True)
    description_text = discord.ui.TextInput(label="Mô tả", style=discord.TextStyle.paragraph, max_length=700, required=False)
    color_text = discord.ui.TextInput(label="Màu HEX", max_length=7, default="#D8A7FF", required=True)

    def __init__(self, data: dict):
        super().__init__()
        self.data = data
        self.name.default = data.get("name") or ""
        self.title_text.default = data.get("embed_title") or ""
        self.description_text.default = data.get("description") or ""
        self.color_text.default = f"#{int(data.get('embed_color') or 0xD8A7FF):06X}"

    async def on_submit(self, interaction: discord.Interaction):
        color = _validate_color(str(self.color_text))
        if color is None:
            await interaction.response.send_message("Màu cần có dạng `#D8A7FF`.", ephemeral=True)
            return
        self.data.update({"name": str(self.name).strip(), "embed_title": str(self.title_text).strip(),
                          "description": str(self.description_text).strip() or None, "embed_color": color})
        await interaction.response.send_message("✅ Nội dung mẫu đã cập nhật. Bấm **Xem trước**.", ephemeral=True)


def template_preview(data: dict) -> discord.Embed:
    embed = discord.Embed(title=f"✨ {data.get('embed_title') or 'Tiêu đề mẫu'}",
                          description=(data.get("description") or "Lời dẫn của bạn sẽ hiển thị ở đây.") + "\n\n🗳️ Hãy chọn đáp án bạn yêu thích nhé!",
                          color=int(data.get("embed_color") or 0xD8A7FF))
    if data.get("author_name"):
        embed.set_author(name=data["author_name"], icon_url=data.get("author_icon_url"))
    else:
        embed.set_author(name="Tên author")
    embed.add_field(name="🌸 Đáp án mẫu", value="`▱▱▱▱▱▱▱▱▱▱`  **0%**  ·  0 phiếu", inline=False)
    embed.set_footer(text=(data.get("footer_text") + "\n" if data.get("footer_text") else "") + "🗳️ 0 phiếu   ·   ⏳ 24 giờ   ·   Chọn một", icon_url=data.get("footer_icon_url"))
    if data.get("thumbnail_url"):
        embed.set_thumbnail(url=data["thumbnail_url"])
    if data.get("image_url"):
        embed.set_image(url=data["image_url"])
    return embed


class TemplateDesignerView(discord.ui.View):
    def __init__(self, owner_id: int):
        super().__init__(timeout=900)
        self.owner_id = owner_id
        self.saved = False
        self.data = {"name": "", "embed_title": "🌙 Tiêu đề poll", "description": "Thêm lời dẫn và emoji cho poll của bạn.",
                     "embed_color": 0xD8A7FF, "thumbnail_url": None, "image_url": None,
                     "author_name": None, "author_icon_url": None, "footer_text": None, "footer_icon_url": None}

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Chỉ người tạo mẫu mới có thể chỉnh sửa.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="✏️ Nội dung", style=discord.ButtonStyle.secondary, row=0)
    async def edit_content(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TemplateBasicModal(self.data))

    @discord.ui.button(label="👤 Author", style=discord.ButtonStyle.secondary, row=0)
    async def edit_author(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AuthorModal(self.data))

    @discord.ui.button(label="📌 Footer", style=discord.ButtonStyle.secondary, row=0)
    async def edit_footer(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(FooterModal(self.data))

    @discord.ui.button(label="🖼️ Ảnh", style=discord.ButtonStyle.secondary, row=0)
    async def edit_images(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ImagesModal(self.data))

    @discord.ui.button(label="👀 Xem trước", style=discord.ButtonStyle.secondary, row=1)
    async def preview(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=template_preview(self.data), view=self)

    @discord.ui.button(label="💾 Lưu mẫu", style=discord.ButtonStyle.success, row=1)
    async def save(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.data.get("name"):
            await interaction.response.send_message("Bấm **Nội dung** và đặt tên mẫu trước khi lưu.", ephemeral=True)
            return
        if self.saved:
            await interaction.response.send_message("Mẫu này đã được lưu rồi.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        payload = {"guild_id": interaction.guild_id, "owner_id": interaction.user.id, **self.data}
        try:
            await interaction.client.save_poll_template(payload)
            self.saved = True
            await interaction.followup.send(f"🎉 Đã lưu mẫu **{self.data['name']}**. Mẫu sẽ được áp dụng mặc định khi tạo poll mới.", ephemeral=True)
        except Exception:
            from app.core.config import log
            log.exception("Poll template creation failed for guild %s", interaction.guild_id)
            await interaction.followup.send("Không lưu được mẫu. Hãy kiểm tra migration Supabase và tên mẫu có bị trùng không.", ephemeral=True)
