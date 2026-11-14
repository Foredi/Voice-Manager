# Discord AI Chatbot v1.0

Trợ lý AI riêng trên Discord, nói tiếng Việt, tự xưng "em" và gọi chủ nhân là
"anh"; thông minh, tận tâm, hơi đanh đá và có bộ nhớ dài hạn. Dự án sử dụng:
- Python 3.11+
- discord.py 2.x
- Google Gen AI SDK (`google-genai`)
- Gemini Developer API
- Supabase PostgreSQL

## Hành vi đã cấu hình

- Chỉ trả lời khi được mention trực tiếp.
- Trợ lý xưng "em", gọi người dùng là "anh"; ưu tiên hỗ trợ thực tế, phản biện
  khi cần và điều chỉnh giọng điệu cho phù hợp tình huống.
- Bộ nhớ dài hạn tách theo `guild_id + user_id`: ký ức của một người ở server A không được dùng ở server B.
- Lịch sử hội thoại tách theo `guild_id + channel_id + user_id`.
- Tự động trích xuất ký ức sau mỗi `MEMORY_EXTRACT_EVERY` tin nhắn của người dùng.
- Lệnh `/memory`, `/forget`, `/reset`, `/chat`, `/about`.
- `/chat` chỉ dành cho người có quyền Manage Server.
- Bot không tự động đọc/đáp các tin nhắn không mention.

## 1. Tạo các dịch vụ

### Discord
1. Tạo application tại https://discord.com/developers/applications
2. Vào Bot, tạo bot và copy token.
3. Bật **Message Content Intent** trong Privileged Gateway Intents.
4. OAuth2 -> URL Generator:
   - Scopes: `bot`, `applications.commands`
   - Bot permissions: View Channels, Send Messages, Read Message History, Embed Links.
5. Mời bot vào server. Không cấp Administrator nếu không cần.

### Gemini
1. Tạo API key tại https://aistudio.google.com/apikey
2. Đặt key vào `GEMINI_API_KEY`.
3. Mặc định `GEMINI_MODEL=gemini-3.8-flash`. Nếu API trả lỗi model not found/not available, kiểm tra model hiện được hỗ trợ trong AI Studio/API docs và đổi biến này.
4. Free tier có quota/rate limit thay đổi theo model, khu vực và tài khoản; không bảo đảm miễn phí không giới hạn.

**Riêng tư:** Lịch sử và ký ức trong cơ sở dữ liệu được phân vùng theo server,
kênh và người dùng; lệnh quản lý dữ liệu chỉ phản hồi riêng tư. Tuy nhiên, tin
nhắn cần thiết để tạo câu trả lời và trích xuất ký ức được gửi tới Gemini API;
theo điều khoản hiện hành, dữ liệu trên free tier có thể được Google dùng để cải
thiện sản phẩm. Không gửi mật khẩu, token, dữ liệu tài chính, dữ liệu sức khỏe
hoặc thông tin nhạy cảm. Không có hệ thống nào ở đây nên được mô tả là bảo mật
tuyệt đối.

### Supabase
1. Tạo project tại https://supabase.com
2. Mở SQL Editor.
3. Chạy toàn bộ `sql/schema.sql`.
4. Lấy Project URL và server-side secret/service_role key trong Project Settings -> API.
5. Chỉ lưu key này trên VPS trong `.env`; tuyệt đối không commit hoặc gửi key vào Discord.

## 2. Cài đặt Ubuntu 24.04

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip unzip

sudo mkdir -p /opt/discord-ai-chatbot
sudo chown -R "$USER":"$USER" /opt/discord-ai-chatbot
cd /opt/discord-ai-chatbot
```

Upload và giải nén source vào thư mục này. Đảm bảo `bot.py` nằm trực tiếp trong `/opt/discord-ai-chatbot/bot.py`, không bị lồng thêm một thư mục cấp hai.

```bash
python3 -m venv venv
source venv/bin/activate
python -m pip install --upgrade pip
pip install --no-cache-dir -r requirements.txt

cp .env.example .env
nano .env
chmod 600 .env
```

Điền đủ `DISCORD_TOKEN`, `GEMINI_API_KEY`, `SUPABASE_URL`, `SUPABASE_KEY`.

## 3. Chạy thử

```bash
source /opt/discord-ai-chatbot/venv/bin/activate
cd /opt/discord-ai-chatbot
python bot.py
```

Khi log báo `Logged in as ...` là bot đã kết nối Discord. Mention bot trong server để thử.

## 4. Chạy 24/7 bằng systemd

Khuyến nghị tạo user riêng:

```bash
sudo useradd --system --home /opt/discord-ai-chatbot --shell /usr/sbin/nologin discordai || true
sudo chown -R discordai:discordai /opt/discord-ai-chatbot
sudo chmod 600 /opt/discord-ai-chatbot/.env
```

Tạo service:

```bash
sudo nano /etc/systemd/system/discord-ai.service
```

Nội dung (nếu VPS 256 MB đang chạy bot khác, không đặt MemoryMax cứng trước khi đo RAM):

```ini
[Unit]
Description=Discord AI Chatbot
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=discordai
Group=discordai
WorkingDirectory=/opt/discord-ai-chatbot
EnvironmentFile=/opt/discord-ai-chatbot/.env
ExecStart=/opt/discord-ai-chatbot/venv/bin/python /opt/discord-ai-chatbot/bot.py
Restart=on-failure
RestartSec=8
TimeoutStopSec=20
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

Kích hoạt:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now discord-ai
sudo systemctl status discord-ai
```

Xem log:

```bash
journalctl -u discord-ai -f
```

Khởi động lại:

```bash
sudo systemctl restart discord-ai
```

## 5. Lệnh Discord

- `@Bot nội dung`: chat.
- `/memory`: xem ký ức dài hạn của bạn trong server hiện tại (chỉ bạn thấy).
- `/forget`: xóa ký ức dài hạn của bạn trong server hiện tại.
- `/reset`: xóa lịch sử chat của bạn với bot trong kênh hiện tại; không xóa ký ức.
- `/chat enabled:true|false`: bật/tắt bot trong server (Manage Server).
- `/about`: thông tin bot.

## 6. Thiết kế dữ liệu

- `guild_users`: định danh người dùng trong server.
- `conversations`: lịch sử theo server + kênh + người dùng.
- `memories`: ký ức dài hạn theo server + người dùng.
- `guild_settings`: trạng thái bật/tắt chatbot của server.

RLS được bật và không có policy công khai. Bot dùng server-side secret key nên chỉ backend trên VPS truy cập Data API.

## 7. Lưu ý vận hành

- Nếu bot không đọc được nội dung mention: kiểm tra Message Content Intent ở Developer Portal và code.
- Nếu lệnh slash chưa xuất hiện: chờ Discord cập nhật hoặc kiểm tra log `Synced application commands`; khởi động lại bot.
- Nếu Gemini báo quota/rate limit: đợi quota reset, giảm tần suất chat hoặc thay model/provider. Free tier không phải quota vô hạn.
- Nếu Gemini báo model không tồn tại: đổi `GEMINI_MODEL` sang model text generation hiện có trong tài khoản.
- Nếu Supabase báo permission/table not found: chạy `sql/schema.sql`, kiểm tra URL/key và Data API đã bật.
- Không chạy hai bản bot cùng một token đồng thời.
- Với VPS 256 MB đang chạy thêm bot khác, kiểm tra `free -h` và `systemd-cgtop`. SDK và thư viện Python có thể khiến RAM vượt giới hạn; nên nâng lên 1 GB nếu muốn chạy ổn định.

## 8. Giới hạn v1.0

- Chưa đọc ảnh/voice/file đính kèm.
- Chưa có dashboard.
- Bộ nhớ trích xuất theo chu kỳ tin nhắn; đây là heuristic tiết kiệm quota, không phải khả năng học liên tục.
- `/forget` xóa ký ức nhưng không xóa lịch sử; `/reset` xóa lịch sử trong kênh hiện tại nhưng không xóa ký ức.
- Nếu cần xóa toàn bộ dữ liệu của một người khỏi hệ thống, cần xóa cả hai loại dữ liệu trong mọi server/kênh liên quan.
