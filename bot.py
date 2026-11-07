import logging
import os
import re
import shutil
import tempfile
import zipfile
from contextlib import asynccontextmanager

import fitz
import uvicorn
from fastapi import FastAPI
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
PORT = int(os.getenv("PORT", "10000"))
MAX_PDF_MB = 20
MAX_FILES_PER_BATCH = 100

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger("pdf-name-bot")

# Pattern matching only; this is not official Aadhaar validation.
NUMBER_RE = re.compile(r"(?<!\d)(?:\d[\s-]*){11}\d(?!\d)")

def extract_candidate(pdf_path: str) -> str | None:
    doc = fitz.open(pdf_path)
    try:
        text = "\n".join(page.get_text("text") for page in doc)
    finally:
        doc.close()

    for match in NUMBER_RE.finditer(text):
        digits = re.sub(r"\D", "", match.group())
        if len(digits) == 12 and len(set(digits)) > 1:
            return digits
    return None

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Each Telegram user gets a separate batch and duplicate set.
    old_dir = context.user_data.get("batch_dir")
    if old_dir:
        shutil.rmtree(old_dir, ignore_errors=True)

    context.user_data["batch_dir"] = tempfile.mkdtemp(prefix="pdfname_")
    context.user_data["files"] = []
    context.user_data["seen"] = set()
    context.user_data["duplicate_count"] = 0
    context.user_data["failed_count"] = 0

    await update.effective_message.reply_text(
        "Welcome to PDF NAME Bot!\n\n"
        "Send PDF documents one by one (max 20 MB each; up to 100 unique PDFs per batch).\n"
        "Exact repeated 12-digit matches are skipped.\n"
        "Unique PDFs are renamed with only the last 4 digits, e.g. PDF_XXXX1234.pdf.\n\n"
        "Send /done to receive a ZIP, or /cancel to discard your batch.\n"
        "Scanned/image-only PDFs are not supported in this version."
    )

async def handle_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    batch_dir = context.user_data.get("batch_dir")
    if not batch_dir:
        await update.effective_message.reply_text("Send /start first to begin your own batch.")
        return

    files = context.user_data.setdefault("files", [])
    if len(files) >= MAX_FILES_PER_BATCH:
        await update.effective_message.reply_text(
            f"Batch limit reached ({MAX_FILES_PER_BATCH} unique PDFs). Send /done."
        )
        return

    document = update.effective_message.document
    if not document or (
        document.mime_type != "application/pdf"
        and not (document.file_name or "").lower().endswith(".pdf")
    ):
        await update.effective_message.reply_text("Please send a PDF as a document.")
        return

    if document.file_size and document.file_size > MAX_PDF_MB * 1024 * 1024:
        await update.effective_message.reply_text(f"PDF exceeds {MAX_PDF_MB} MB.")
        return

    temp_path = os.path.join(batch_dir, f"{document.file_unique_id}.upload")
    try:
        tg_file = await context.bot.get_file(document.file_id)
        await tg_file.download_to_drive(temp_path)
        candidate = extract_candidate(temp_path)
    except Exception:
        log.exception("PDF processing failed")
        if os.path.exists(temp_path):
            os.remove(temp_path)
        await update.effective_message.reply_text(
            "Could not read this PDF. Please check that it is valid and text-readable."
        )
        return

    if not candidate:
        os.remove(temp_path)
        context.user_data["failed_count"] = context.user_data.get("failed_count", 0) + 1
        await update.effective_message.reply_text(
            "No readable 12-digit number found. Scanned/image-only PDFs need OCR."
        )
        return

    seen = context.user_data.setdefault("seen", set())
    if candidate in seen:
        os.remove(temp_path)
        context.user_data["duplicate_count"] = context.user_data.get("duplicate_count", 0) + 1
        await update.effective_message.reply_text("Duplicate match — this PDF was skipped.")
        return

    seen.add(candidate)
    filename = f"PDF_XXXX{candidate[-4:]}.pdf"
    output_path = os.path.join(batch_dir, filename)
    suffix = 2
    while os.path.exists(output_path):
        filename = f"PDF_XXXX{candidate[-4:]}_{suffix}.pdf"
        output_path = os.path.join(batch_dir, filename)
        suffix += 1

    os.replace(temp_path, output_path)
    files.append(output_path)
    await update.effective_message.reply_text(f"Added: {filename}")

async def done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    batch_dir = context.user_data.get("batch_dir")
    files = context.user_data.get("files", [])
    if not batch_dir or not files:
        await update.effective_message.reply_text("No unique PDFs ready. Send /start first.")
        return

    zip_path = os.path.join(batch_dir, "PDF_NAME_unique_files.zip")
    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in files:
                archive.write(path, arcname=os.path.basename(path))

        with open(zip_path, "rb") as archive_file:
            await update.effective_message.reply_document(
                document=archive_file,
                filename="PDF_NAME_unique_files.zip",
                caption=(
                    f"Finished: {len(files)} unique PDF(s).\n"
                    f"Duplicates skipped: {context.user_data.get('duplicate_count', 0)}\n"
                    f"Unreadable/no-match: {context.user_data.get('failed_count', 0)}"
                )
            )
    finally:
        shutil.rmtree(batch_dir, ignore_errors=True)
        for key in ("batch_dir", "files", "seen", "duplicate_count", "failed_count"):
            context.user_data.pop(key, None)

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    batch_dir = context.user_data.pop("batch_dir", None)
    if batch_dir:
        shutil.rmtree(batch_dir, ignore_errors=True)
    for key in ("files", "seen", "duplicate_count", "failed_count"):
        context.user_data.pop(key, None)
    await update.effective_message.reply_text("Your batch was cancelled and temporary files deleted.")

telegram_app = Application.builder().token(BOT_TOKEN).build()

@asynccontextmanager
async def lifespan(api: FastAPI):
    if not BOT_TOKEN:
        raise RuntimeError("Missing BOT_TOKEN environment variable.")

    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(CommandHandler("done", done))
    telegram_app.add_handler(CommandHandler("cancel", cancel))
    telegram_app.add_handler(MessageHandler(filters.Document.ALL, handle_pdf))

    await telegram_app.initialize()
    await telegram_app.start()
    await telegram_app.updater.start_polling(drop_pending_updates=True)
    log.info("Public Telegram bot polling started")
    try:
        yield
    finally:
        await telegram_app.updater.stop()
        await telegram_app.stop()
        await telegram_app.shutdown()

api = FastAPI(lifespan=lifespan)

@api.get("/")
async def home():
    return {"name": "PDF NAME", "status": "healthy", "bot": "public"}

@api.get("/health")
async def health():
    return {"status": "ok"}

if __name__ == "__main__":
    uvicorn.run(api, host="0.0.0.0", port=PORT)
