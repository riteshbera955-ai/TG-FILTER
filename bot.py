import os
import re
import shutil
import tempfile
import zipfile
from pathlib import Path

import fitz
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters
)

BOT_TOKEN = os.getenv("BOT_TOKEN")
AADHAAR_RE = re.compile(r"(?<!\d)(?:\d[\s-]*){11}\d(?!\d)")

def find_aadhaar(pdf_path: str) -> str | None:
    """Return a 12-digit candidate from selectable PDF text."""
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
        "Send PDF files one by one. Exact duplicate ID numbers are skipped; "
        "output filenames show only the last 4 digits (Aadhaar_XXXX1234.pdf). "
        "When finished, send /done. Send /cancel to discard the batch."
    )

async def handle_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    batch_dir = context.user_data.get("batch_dir")
    if not batch_dir:
        await update.message.reply_text("First send /start.")
        return

    doc_msg = update.message.document
    if not doc_msg or doc_msg.mime_type != "application/pdf":
        await update.message.reply_text("Please send a PDF document.")
        return
    if doc_msg.file_size and doc_msg.file_size > 20 * 1024 * 1024:
        await update.message.reply_text("This PDF is larger than 20 MB.")
        return

    tg_file = await context.bot.get_file(doc_msg.file_id)
    original_path = os.path.join(batch_dir, str(doc_msg.file_unique_id) + ".pdf")
    await tg_file.download_to_drive(original_path)

    try:
        aadhaar = find_aadhaar(original_path)
    except Exception:
        os.remove(original_path)
        await update.message.reply_text("Could not read this PDF. Please check that it is valid.")
        return

    if not aadhaar:
        os.remove(original_path)
        await update.message.reply_text(
            "No readable 12-digit number found. Scanned/image-only PDFs are not supported in this version."
        )
        return

    # Compare the complete number in memory to identify exact duplicates.
    # The complete number is never included in the reply or filename.
    seen_ids = context.user_data.setdefault("seen_ids", set())
    if aadhaar in seen_ids:
        os.remove(original_path)
        await update.message.reply_text("Duplicate number found — this PDF was skipped.")
        return

    seen_ids.add(aadhaar)
    last4 = aadhaar[-4:]
    output_name = f"Aadhaar_XXXX{last4}.pdf"
    output_path = os.path.join(batch_dir, output_name)

    counter = 2
    while os.path.exists(output_path):
        output_name = f"Aadhaar_XXXX{last4}_{counter}.pdf"
        output_path = os.path.join(batch_dir, output_name)
        counter += 1

    os.replace(original_path, output_path)
    context.user_data.setdefault("files", []).append(output_path)
    await update.message.reply_text(f"Added: {output_name}")

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

    with open(zip_path, "rb") as f:
        await update.message.reply_document(
            document=f,
            filename="unique_renamed_pdfs.zip",
            caption=f"Finished: {len(files)} unique PDF(s) included. Exact duplicates skipped."
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

def main():
    if not BOT_TOKEN:
        raise RuntimeError("Set BOT_TOKEN as an environment variable.")
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("done", done))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(MessageHandler(filters.Document.PDF, handle_pdf))
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
