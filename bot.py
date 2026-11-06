import asyncio
import os
import re
import shutil
import tempfile
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path

import fitz
import uvicorn
from fastapi import FastAPI
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters
)

BOT_TOKEN = os.getenv("BOT_TOKEN")
PORT = int(os.getenv("PORT", "10000"))
AADHAAR_RE = re.compile(r"(?<!\d)(?:\d[\s-]*){11}\d(?!\d)")

def find_number(pdf_path: str) -> str | None:
    """Find a 12-digit candidate in selectable PDF text."""
    doc = fitz.open(pdf_path)
    try:
        text = "\n".join(page.get_text("text") for page in doc)
    finally:
        doc.close()

    for match in AADHAAR_RE.finditer(text):
        digits = re.sub(r"\D", "", match.group())
        if len(digits) == 12 and len(set(digits)) > 1:
            return digits
    return None

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    old_dir = context.user_data.get("batch_dir")
    if old_dir:
        shutil.rmtree(old_dir, ignore_errors=True)
    batch_dir = tempfile.mkdtemp(prefix="pdf_batch_")
    context.user_data["batch_dir"] = batch_dir
    context.user_data["files"] = []
    context.user_data["seen_ids"] = set()
    await update.message.reply_text(
        "Send PDF files one by one. Exact duplicate numbers are skipped. "
        "Files are named with only the last 4 digits (Aadhaar_XXXX1234.pdf). "
        "Send /done when finished or /cancel to discard this batch."
    )

async def handle_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    batch_dir = context.user_data.get("batch_dir")
    if not batch_dir:
        await update.message.reply_text("First send /start.")
        return

    document = update.message.document
    if not document or document.mime_type != "application/pdf":
        await update.message.reply_text("Please send a PDF document.")
        return
    if document.file_size and document.file_size > 20 * 1024 * 1024:
        await update.message.reply_text("This PDF is larger than 20 MB.")
        return

    telegram_file = await context.bot.get_file(document.file_id)
    original_path = os.path.join(batch_dir, f"{document.file_unique_id}.pdf")
    await telegram_file.download_to_drive(original_path)

    try:
        number = find_number(original_path)
    except Exception:
        if os.path.exists(original_path):
            os.remove(original_path)
        await update.message.reply_text("Could not read this PDF. Please check that it is valid.")
        return

    if not number:
        os.remove(original_path)
        await update.message.reply_text(
            "No readable 12-digit number found. Scanned/image-only PDFs need OCR "
            "and are not supported in this version."
        )
        return

    seen = context.user_data.setdefault("seen_ids", set())
    if number in seen:
        os.remove(original_path)
        await update.message.reply_text("Duplicate number found — this PDF was skipped.")
        return

    seen.add(number)
    filename = f"Aadhaar_XXXX{number[-4:]}.pdf"
    output_path = os.path.join(batch_dir, filename)
    suffix = 2
    while os.path.exists(output_path):
        filename = f"Aadhaar_XXXX{number[-4:]}_{suffix}.pdf"
        output_path = os.path.join(batch_dir, filename)
        suffix += 1

    os.replace(original_path, output_path)
    context.user_data.setdefault("files", []).append(output_path)
    await update.message.reply_text(f"Added: {filename}")

async def done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    files = context.user_data.get("files", [])
    batch_dir = context.user_data.get("batch_dir")
    if not files or not batch_dir:
        await update.message.reply_text("No unique PDFs processed yet. Send /start first.")
        return

    zip_path = os.path.join(batch_dir, "unique_renamed_pdfs.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, arcname=os.path.basename(path))

    with open(zip_path, "rb") as archive_file:
        await update.message.reply_document(
            document=archive_file,
            filename="unique_renamed_pdfs.zip",
            caption=f"Finished: {len(files)} unique PDF(s). Exact duplicates skipped."
        )

    shutil.rmtree(batch_dir, ignore_errors=True)
    context.user_data.pop("batch_dir", None)
    context.user_data.pop("files", None)
    context.user_data.pop("seen_ids", None)

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    batch_dir = context.user_data.pop("batch_dir", None)
    context.user_data.pop("files", None)
    context.user_data.pop("seen_ids", None)
    if batch_dir:
        shutil.rmtree(batch_dir, ignore_errors=True)
    await update.message.reply_text("Batch cancelled; temporary files deleted.")

telegram_app = Application.builder().token(BOT_TOKEN).build()

@asynccontextmanager
async def lifespan(api: FastAPI):
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is missing.")

    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(CommandHandler("done", done))
    telegram_app.add_handler(CommandHandler("cancel", cancel))
    telegram_app.add_handler(MessageHandler(filters.Document.PDF, handle_pdf))

    await telegram_app.initialize()
    await telegram_app.start()
    if telegram_app.updater is None:
        raise RuntimeError("Telegram updater is unavailable.")
    await telegram_app.updater.start_polling(drop_pending_updates=True)

    try:
        yield
    finally:
        await telegram_app.updater.stop()
        await telegram_app.stop()
        await telegram_app.shutdown()

api = FastAPI(lifespan=lifespan)

@api.get("/")
async def home():
    return {"status": "healthy", "service": "Telegram PDF Renamer Bot"}

@api.get("/health")
async def health():
    return {"status": "ok"}

if __name__ == "__main__":
    uvicorn.run(api, host="0.0.0.0", port=PORT)
