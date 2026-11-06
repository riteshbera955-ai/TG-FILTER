import os
import re
import io
import json
import shutil
import zipfile
import logging
import tempfile
import threading
from pathlib import Path
from collections import defaultdict

import fitz  # PyMuPDF
from flask import Flask
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters
)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
PORT = int(os.environ.get("PORT", "10000"))
MAX_PDF_BYTES = 20 * 1024 * 1024  # Telegram cloud Bot API download limit is commonly 20 MB
NUMBER_RE = re.compile(r"(?<!\d)\d{12}(?!\d)")

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO
)
log = logging.getLogger("pdf-filter-bot")

app_health = Flask(__name__)

@app_health.get("/")
@app_health.get("/health")
def health():
    return {"status": "ok", "service": "PDF Number Filter Bot"}, 200

# Per-user temporary upload queues. Files are processed only after /process.
user_queues = defaultdict(list)
queue_lock = threading.Lock()


def extract_one_number(pdf_path: Path):
    """Return (number, reason). OCR is intentionally not used."""
    try:
        doc = fitz.open(pdf_path)
        if doc.is_encrypted:
            return None, "password-protected PDF"
        chunks = []
        for page in doc:
            chunks.append(page.get_text("text") or "")
        doc.close()
        text = "\n".join(chunks)

        if not text.strip():
            return None, "no selectable text (likely scanned/image PDF)"

        matches = NUMBER_RE.findall(text)
        unique = sorted(set(matches))

        if len(unique) == 0:
            return None, "no 12-digit number found"
        if len(unique) > 1:
            return None, "more than one different 12-digit number found"
        return unique[0], None
    except Exception as exc:
        log.exception("PDF parsing failed")
        return None, f"could not read PDF: {type(exc).__name__}"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📄 PDF Number Filter Bot\n\n"
        "Send me one or more text-based PDF files, then use /process.\n"
        "For each PDF, I look for exactly one distinct 12-digit number and "
        "rename the PDF to NUMBER.pdf.\n\n"
        "Commands:\n"
        "/process — process uploaded PDFs\n"
        "/count — show queued PDF count\n"
        "/clear — delete your queued PDFs\n\n"
        "Scanned/image PDFs are skipped; OCR is not enabled. "
        "Only upload PDFs you are authorized to process."
    )


async def receive_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    doc = message.document
    if not doc:
        return

    filename = (doc.file_name or "upload.pdf")
    if not filename.lower().endswith(".pdf") or doc.mime_type not in (None, "application/pdf"):
        await message.reply_text("Please send a PDF document.")
        return

    if doc.file_size and doc.file_size > MAX_PDF_BYTES:
        await message.reply_text("This PDF is too large for this bot's current download limit (20 MB).")
        return

    user_id = update.effective_user.id
    user_dir = Path(tempfile.gettempdir()) / "pdf_number_filter" / str(user_id)
    user_dir.mkdir(parents=True, exist_ok=True)

    # Use a unique local name; never trust the uploaded filename as a path.
    local_path = user_dir / f"{doc.file_unique_id}.pdf"
    try:
        tg_file = await context.bot.get_file(doc.file_id)
        await tg_file.download_to_drive(custom_path=str(local_path))
    except Exception:
        log.exception("Download failed")
        await message.reply_text("I couldn't download that PDF. Please try again.")
        return

    with queue_lock:
        user_queues[user_id].append({
            "path": str(local_path),
            "original_name": Path(filename).name
        })

    await message.reply_text(
        f"✅ Added: {Path(filename).name}\n"
        f"Queued PDFs: {len(user_queues[user_id])}\n"
        "Send more PDFs or use /process."
    )


async def count_queue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with queue_lock:
        count = len(user_queues[update.effective_user.id])
    await update.message.reply_text(f"Queued PDFs: {count}")


async def clear_queue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    with queue_lock:
        items = user_queues.pop(user_id, [])
    for item in items:
        try:
            Path(item["path"]).unlink(missing_ok=True)
        except Exception:
            pass
    user_dir = Path(tempfile.gettempdir()) / "pdf_number_filter" / str(user_id)
    shutil.rmtree(user_dir, ignore_errors=True)
    await update.message.reply_text(f"🗑️ Cleared {len(items)} queued PDF(s).")


async def process_queue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    with queue_lock:
        items = user_queues.pop(user_id, [])

    if not items:
        await update.message.reply_text("No PDFs queued. Send PDF files first.")
        return

    status = await update.message.reply_text(f"⏳ Processing {len(items)} PDF(s)...")

    work_dir = Path(tempfile.mkdtemp(prefix=f"pdf_filter_{user_id}_"))
    output_dir = work_dir / "renamed_pdfs"
    output_dir.mkdir()
    used_numbers = set()
    successful = []
    skipped = []

    try:
        for item in items:
            source = Path(item["path"])
            if not source.exists():
                skipped.append((item["original_name"], "temporary upload missing"))
                continue

            number, reason = extract_one_number(source)
            if reason:
                skipped.append((item["original_name"], reason))
                continue

            if number in used_numbers:
                skipped.append((item["original_name"], f"duplicate number {number}"))
                continue

            used_numbers.add(number)
            target = output_dir / f"{number}.pdf"
            shutil.copy2(source, target)
            successful.append((number, target))

        summary_lines = [
            "PDF Number Filter — Results",
            f"PDFs received: {len(items)}",
            f"PDFs renamed/exported: {len(successful)}",
            f"PDFs skipped: {len(skipped)}",
            "",
            "Unique 12-digit numbers:"
        ]
        summary_lines.extend(number for number, _ in successful)
        summary_lines.extend(["", "Skipped files:"])
        summary_lines.extend(
            f"{name} — {reason}" for name, reason in skipped
        )
        txt_path = work_dir / "results.txt"
        txt_path.write_text("\n".join(summary_lines), encoding="utf-8")

        if successful:
            zip_path = work_dir / "renamed_pdfs.zip"
            with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                for _, path in successful:
                    zf.write(path, arcname=path.name)

            with open(zip_path, "rb") as f:
                await update.message.reply_document(
                    document=f,
                    filename="renamed_pdfs.zip",
                    caption=f"✅ Done: {len(successful)} renamed PDF(s)."
                )
        else:
            await update.message.reply_text(
                "No PDFs qualified. Check results.txt for skip reasons."
            )

        with open(txt_path, "rb") as f:
            await update.message.reply_document(
                document=f,
                filename="results.txt",
                caption=(
                    f"Found: {len(successful)} | "
                    f"Skipped: {len(skipped)}"
                )
            )

        await status.edit_text(
            f"Finished.\n"
            f"Renamed: {len(successful)}\n"
            f"Skipped: {len(skipped)}"
        )
    except Exception:
        log.exception("Processing failed")
        await status.edit_text("An error occurred while processing. Please try again.")
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
        # Remove original queued files after processing.
        for item in items:
            try:
                Path(item["path"]).unlink(missing_ok=True)
            except Exception:
                pass


def run_health_server():
    app_health.run(host="0.0.0.0", port=PORT, use_reloader=False)


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is missing.")

    threading.Thread(target=run_health_server, daemon=True).start()

    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("count", count_queue))
    application.add_handler(CommandHandler("clear", clear_queue))
    application.add_handler(CommandHandler("process", process_queue))
    application.add_handler(MessageHandler(filters.Document.ALL, receive_pdf))

    log.info("Starting Telegram polling bot")
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
