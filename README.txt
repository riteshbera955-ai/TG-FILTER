PDF NAME — Public Telegram PDF-to-JPG Bot

Render Web Service:
Build: pip install -r requirements.txt
Start: python bot.py
Environment variable: BOT_TOKEN = your BotFather token
Python: 3.13.5 (.python-version)

Anyone can use the bot. Each Telegram user has a separate batch.
Commands: /start, /done, /cancel.
Each accepted PDF page is converted to a JPG at 200 DPI; multipage PDFs
produce _page1.jpg, _page2.jpg, etc. JPG files are returned in one ZIP.
Duplicate 12-digit candidates are skipped within each user's batch.
Output filenames show only the last four digits (PDF_XXXX1234.jpg).

Limit: 20 MB per PDF, 100 unique PDFs per batch.
Text-based detection only; scanned/image-only PDFs need OCR.
This is pattern matching, not official Aadhaar validation.
