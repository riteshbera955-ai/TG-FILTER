# Telegram PDF Number Filter Bot

## What it does
- Accepts one or more PDF documents in Telegram.
- Extracts selectable text from each PDF (no OCR).
- Accepts a PDF only when it contains exactly one **distinct** 12-digit number.
- Renames accepted PDFs to `123456789012.pdf`.
- Skips duplicate numbers across the uploaded batch.
- Returns a ZIP of renamed PDFs and a TXT report.

## Deploy on Render
1. Create a Telegram bot with **@BotFather** and copy its token.
2. Upload these files to a GitHub repository.
3. In Render, create a **New Web Service** and connect the repository.
4. Use:
   - Build command: `pip install -r requirements.txt`
   - Start command: `python bot.py`
5. Add an environment variable:
   - Key: `BOT_TOKEN`
   - Value: your BotFather token
6. Deploy, then open your Telegram bot and send `/start`.

You can also use the included `render.yaml` as a Blueprint configuration.

## How to use
1. Send PDF files to the bot as **documents** (not screenshots).
2. After sending all PDFs, send `/process`.
3. The bot returns `renamed_pdfs.zip` and `results.txt`.
4. Use `/count` to see queued files or `/clear` to delete the queue.

## Rules
- A PDF must contain selectable text. Image-only/scanned PDFs are skipped.
- A PDF must contain exactly one distinct 12-digit number. If it contains zero or multiple different 12-digit numbers, it is skipped.
- If the same 12-digit number appears in more than one PDF in a batch, only the first PDF is exported.
- The bot does not use OCR.
- Current per-file download limit is 20 MB.
- Upload only documents you own or are authorized to process. The bot temporarily stores uploaded files while processing and deletes them after `/process` or `/clear`.

## Local run
```bash
pip install -r requirements.txt
# Linux/macOS:
export BOT_TOKEN="YOUR_TELEGRAM_BOT_TOKEN"
# Windows PowerShell:
# $env:BOT_TOKEN="YOUR_TELEGRAM_BOT_TOKEN"
python bot.py
```
