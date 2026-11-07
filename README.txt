PDF NAME — Public Telegram Bot (Render Free Web Service)

Anyone can open the Telegram bot and use it. There is no ALLOWED_USER_ID setting.
Each Telegram account has its own separate batch and duplicate-detection list.

Render:
- Service: Web Service
- Build command: pip install -r requirements.txt
- Start command: python bot.py
- Environment variable: BOT_TOKEN = token from @BotFather
- Python version: .python-version (3.13.5)

Commands:
- /start: start a private batch
- send PDF documents one by one
- /done: receive a ZIP of unique renamed PDFs
- /cancel: delete the current batch

Limits:
- 20 MB per PDF
- 100 unique PDFs per batch
- selectable-text PDFs only; scanned PDFs require OCR
