import os
import re
import shutil
import zipfile
import logging
import tempfile
import threading

from pathlib import Path
from collections import defaultdict

import fitz
from flask import Flask
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters
)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
PORT = int(os.environ.get("PORT", "10000"))

MAX_PDF_BYTES = 20 * 1024 * 1024

# Match 12 digits, allowing spaces or hyphens between digits
NUMBER_RE = re.compile(r"(?<!\d)(?:\d[ -]?){11}\d(?!\d)")

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO
)

log = logging.getLogger("pdf-filter-bot")

app_health = Flask(__name__)

@app_health.get("/")
@app_health.get("/health")
def health():
    return {
        "status": "ok",
        "service": "PDF Number Filter Bot"
    }, 200


user_queues = defaultdict(list)
queue_lock = threading.Lock()


def extract_one_number(pdf_path: Path):
    """Extract exactly one distinct 12-digit number from a text PDF."""

    try:
        doc = fitz.open(pdf_path)

        if doc.is_encrypted:
            return None, "Password-protected PDF"

        chunks = []

        for page in doc:
            chunks.append(page.get_text("text") or "")

        doc.close()

        text = "\n".join(chunks)

        if not text.strip():
            return None, "Scanned or image PDF"

        matches = [
            re.sub(r"[ -]", "", value)
            for value in NUMBER_RE.findall(text)
        ]

        unique = sorted(set(matches))

        if len(unique) == 0:
            return None, "No 12-digit number found"

        if len(unique) > 1:
            return None, "Multiple different numbers found"

        return unique[0], None

    except Exception as exc:
        log.exception("PDF parsing failed")
        return None, f"Could not read PDF: {type(exc).__name__}"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await update.message.reply_text(
        "📄 PDF NUMBER FILTER BOT\n\n"

        "Send one or more PDF files.\n\n"

        "Commands:\n"
        "/process - Process PDFs\n"
        "/count - Check uploaded PDFs\n"
        "/clear - Clear uploaded PDFs\n\n"

        "The bot extracts one unique 12-digit number "
        "from each PDF and renames the PDF.\n\n"

        "Scanned PDFs are not supported."
    )


async def receive_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):

    message = update.message
    doc = message.document

    if not doc:
        return

    filename = doc.file_name or "upload.pdf"

    if not filename.lower().endswith(".pdf"):
        await message.reply_text("Please send a PDF file.")
        return

    if doc.file_size and doc.file_size > MAX_PDF_BYTES:
        await message.reply_text("PDF is too large. Maximum size is 20 MB.")
        return

    user_id = update.effective_user.id

    user_dir = (
        Path(tempfile.gettempdir())
        / "pdf_number_filter"
        / str(user_id)
    )

    user_dir.mkdir(parents=True, exist_ok=True)

    local_path = user_dir / f"{doc.file_unique_id}.pdf"

    try:
        tg_file = await context.bot.get_file(doc.file_id)

        await tg_file.download_to_drive(
            custom_path=str(local_path)
        )

    except Exception:
        log.exception("Download failed")

        await message.reply_text(
            "Could not download PDF. Please try again."
        )

        return

    with queue_lock:

        user_queues[user_id].append({
            "path": str(local_path),
            "original_name": Path(filename).name
        })

        count = len(user_queues[user_id])

    await message.reply_text(
        f"✅ PDF Added!\n"
        f"File: {filename}\n"
        f"Total queued: {count}\n\n"
        f"Send more PDFs or type /process."
    )


async def count_queue(update: Update, context: ContextTypes.DEFAULT_TYPE):

    with queue_lock:
        count = len(user_queues[update.effective_user.id])

    await update.message.reply_text(
        f"📂 Queued PDFs: {count}"
    )


async def clear_queue(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user_id = update.effective_user.id

    with queue_lock:
        items = user_queues.pop(user_id, [])

    for item in items:
        try:
            Path(item["path"]).unlink(missing_ok=True)
        except Exception:
            pass

    user_dir = (
        Path(tempfile.gettempdir())
        / "pdf_number_filter"
        / str(user_id)
    )

    shutil.rmtree(user_dir, ignore_errors=True)

    await update.message.reply_text(
        f"🗑️ Cleared {len(items)} PDFs."
    )


async def process_queue(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user_id = update.effective_user.id

    with queue_lock:
        items = user_queues.pop(user_id, [])

    if not items:
        await update.message.reply_text(
            "No PDFs uploaded. Send PDF files first."
        )
        return

    status = await update.message.reply_text(
        f"⏳ Processing {len(items)} PDFs..."
    )

    work_dir = Path(
        tempfile.mkdtemp(prefix=f"pdf_filter_{user_id}_")
    )

    output_dir = work_dir / "renamed_pdfs"
    output_dir.mkdir()

    used_numbers = set()
    successful = []
    skipped = []

    try:

        for item in items:

            source = Path(item["path"])

            if not source.exists():
                skipped.append(
                    (item["original_name"], "Temporary file missing")
                )
                continue

            number, reason = extract_one_number(source)

            if reason:
                skipped.append(
                    (item["original_name"], reason)
                )
                continue

            if number in used_numbers:
                skipped.append(
                    (item["original_name"], f"Duplicate number: {number}")
                )
                continue

            used_numbers.add(number)

            target = output_dir / f"{number}.pdf"

            shutil.copy2(source, target)

            successful.append((number, target))

        # Create TXT report
        summary_lines = [
            "PDF NUMBER FILTER RESULTS",
            f"Total PDFs: {len(items)}",
            f"Successful: {len(successful)}",
            f"Skipped: {len(skipped)}",
            "",
            "UNIQUE NUMBERS:"
        ]

        summary_lines.extend(
            number for number, _ in successful
        )

        summary_lines.extend([
            "",
            "SKIPPED FILES:"
        ])

        summary_lines.extend(
            f"{name} - {reason}"
            for name, reason in skipped
        )

        txt_path = work_dir / "results.txt"

        txt_path.write_text(
            "\n".join(summary_lines),
            encoding="utf-8"
        )

        # Create ZIP
        if successful:

            zip_path = work_dir / "renamed_pdfs.zip"

            with zipfile.ZipFile(
                zip_path,
                "w",
                compression=zipfile.ZIP_DEFLATED
            ) as zf:

                for _, path in successful:
                    zf.write(
                        path,
                        arcname=path.name
                    )

            with open(zip_path, "rb") as f:

                await update.message.reply_document(
                    document=f,
                    filename="renamed_pdfs.zip",
                    caption=f"✅ Renamed PDFs: {len(successful)}"
                )

        else:

            await update.message.reply_text(
                "No valid PDFs found."
            )

        # Send report
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
            f"✅ Processing Complete!\n\n"
            f"Renamed: {len(successful)}\n"
            f"Skipped: {len(skipped)}"
        )

    except Exception:

        log.exception("Processing failed")

        await status.edit_text(
            "❌ Processing failed. Please try again."
        )

    finally:

        shutil.rmtree(work_dir, ignore_errors=True)

        for item in items:
            try:
                Path(item["path"]).unlink(missing_ok=True)
            except Exception:
                pass


def run_health_server():

    app_health.run(
        host="0.0.0.0",
        port=PORT,
        use_reloader=False
    )


def main():

    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN environment variable is missing."
        )

    threading.Thread(
        target=run_health_server,
        daemon=True
    ).start()

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    application.add_handler(
        CommandHandler("start", start)
    )

    application.add_handler(
        CommandHandler("count", count_queue)
    )

    application.add_handler(
        CommandHandler("clear", clear_queue)
    )

    application.add_handler(
        CommandHandler("process", process_queue)
    )

    application.add_handler(
        MessageHandler(
            filters.Document.ALL,
            receive_pdf
        )
    )

    log.info("Starting PDF Number Filter Bot")

    application.run_polling(
        drop_pending_updates=True
    )


if __name__ == "__main__":
    main()
