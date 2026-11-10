import asyncio
import hashlib
import logging
import os
import shutil
import tempfile
import zipfile
from contextlib import asynccontextmanager
from pathlib import PurePosixPath

import fitz
import uvicorn
from fastapi import FastAPI
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
PORT = int(os.getenv("PORT", "10000"))

MAX_FILE_MB = 20
MAX_ITEMS_PER_BATCH = 100
MAX_ZIP_UNCOMPRESSED_MB = 200
IMAGE_EXTENSIONS = {".jpg", ".jpeg"}

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("pdf-zip")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def jpg_bytes_to_pdf(image_data: bytes) -> bytes:
    """Convert one JPG/JPEG image into a one-page PDF."""
    image_doc = fitz.open(stream=image_data, filetype="jpg")
    try:
        return image_doc.convert_to_pdf()
    finally:
        image_doc.close()


def add_pdf(data: dict, filename: str, pdf_bytes: bytes):
    """Save a PDF into this user's current batch, skipping exact duplicates."""
    digest = sha256_bytes(pdf_bytes)
    if digest in data["hashes"]:
        data["duplicates"] += 1
        return False

    # Use a safe, predictable PDF filename.
    stem = PurePosixPath(filename).stem or f"PDF_{len(data['pdfs']) + 1:03d}"
    safe_stem = "".join(c for c in stem if c.isalnum() or c in ("-", "_", " ")).strip()
    safe_stem = safe_stem[:70] or f"PDF_{len(data['pdfs']) + 1:03d}"
    output_name = f"{safe_stem}.pdf"

    existing = {item[0] for item in data["pdfs"]}
    suffix = 2
    while output_name in existing:
        output_name = f"{safe_stem}_{suffix}.pdf"
        suffix += 1

    output_path = os.path.join(data["folder"], output_name)
    with open(output_path, "wb") as f:
        f.write(pdf_bytes)

    data["hashes"].add(digest)
    data["pdfs"].append((output_name, output_path))
    return True


def process_jpg_zip(zip_path: str, data: dict):
    """Read JPG/JPEG files from a ZIP and convert each image to a separate PDF."""
    added = 0
    total_uncompressed = 0

    with zipfile.ZipFile(zip_path, "r") as archive:
        image_entries = [
            info for info in archive.infolist()
            if not info.is_dir()
            and PurePosixPath(info.filename).suffix.lower() in IMAGE_EXTENSIONS
        ]

        if not image_entries:
            raise ValueError("This ZIP contains no JPG/JPEG images.")

        if len(image_entries) > MAX_ITEMS_PER_BATCH - len(data["pdfs"]):
            raise ValueError(
                f"Too many images. This batch can contain at most "
                f"{MAX_ITEMS_PER_BATCH} PDF files."
            )

        for info in image_entries:
            total_uncompressed += info.file_size
            if total_uncompressed > MAX_ZIP_UNCOMPRESSED_MB * 1024 * 1024:
                raise ValueError(
                    f"ZIP contents exceed the {MAX_ZIP_UNCOMPRESSED_MB} MB safety limit."
                )

            image_data = archive.read(info)
            pdf_data = jpg_bytes_to_pdf(image_data)
            if add_pdf(data, PurePosixPath(info.filename).name, pdf_data):
                added += 1

    return added, len(image_entries)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    old_folder = context.user_data.get("folder")
    if old_folder:
        shutil.rmtree(old_folder, ignore_errors=True)

    context.user_data.clear()
    folder = tempfile.mkdtemp(prefix=f"pdfzip_{update.effective_user.id}_")
    context.user_data.update(
        folder=folder,
        pdfs=[],
        hashes=set(),
        received=0,
        duplicates=0,
        failed=0,
        lock=asyncio.Lock(),
    )

    await update.effective_message.reply_text(
        "PDF ZIP bot is ready.\n\n"
        "• Send PDF documents: they will be packed into a PDF ZIP.\n"
        "• Send a ZIP containing JPG/JPEG images: each image will be converted "
        "to a separate PDF, then packed into a PDF ZIP.\n"
        f"• Maximum {MAX_ITEMS_PER_BATCH} PDFs per batch; individual upload limit "
        f"{MAX_FILE_MB} MB.\n"
        "• Exact duplicate PDF files are skipped.\n\n"
        "Send /done to receive the ZIP. After /done, you can send more files "
        "without using /start again.\n"
        "Send /cancel to end the session."
    )


async def receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = context.user_data
    if not data.get("folder"):
        await update.effective_message.reply_text("Please send /start first.")
        return

    doc = update.effective_message.document
    if not doc:
        return

    filename = doc.file_name or "upload"
    extension = os.path.splitext(filename)[1].lower()

    if extension not in {".pdf", ".zip"} and doc.mime_type not in {
        "application/pdf", "application/zip", "application/x-zip-compressed"
    }:
        await update.effective_message.reply_text(
            "Please send a PDF file or a ZIP containing JPG/JPEG images as a document."
        )
        return

    if doc.file_size and doc.file_size > MAX_FILE_MB * 1024 * 1024:
        await update.effective_message.reply_text(f"File exceeds {MAX_FILE_MB} MB.")
        return

    async with data["lock"]:
        if len(data["pdfs"]) >= MAX_ITEMS_PER_BATCH:
            await update.effective_message.reply_text(
                f"Batch limit reached ({MAX_ITEMS_PER_BATCH} PDFs). Send /done first."
            )
            return
        data["received"] += 1
        sequence = data["received"]

    source = os.path.join(data["folder"], f"upload_{sequence:03d}{extension or '.bin'}")

    try:
        telegram_file = await context.bot.get_file(doc.file_id)
        await telegram_file.download_to_drive(source)

        if extension == ".pdf" or doc.mime_type == "application/pdf":
            with open(source, "rb") as f:
                pdf_bytes = f.read()
            # Validate the uploaded file is a readable PDF.
            check = fitz.open(stream=pdf_bytes, filetype="pdf")
            check.close()

            async with data["lock"]:
                if len(data["pdfs"]) >= MAX_ITEMS_PER_BATCH:
                    raise ValueError(f"Batch limit is {MAX_ITEMS_PER_BATCH} PDFs.")
                added = add_pdf(data, filename, pdf_bytes)

            if added:
                await update.effective_message.reply_text(
                    f"PDF received: {filename}\nAdded to ZIP. Total PDFs: {len(data['pdfs'])}."
                )
            else:
                await update.effective_message.reply_text("Exact duplicate PDF skipped.")

        elif extension == ".zip" or doc.mime_type in {
            "application/zip", "application/x-zip-compressed"
        }:
            async with data["lock"]:
                added, found = await asyncio.to_thread(process_jpg_zip, source, data)

            await update.effective_message.reply_text(
                f"JPG ZIP processed: {found} image(s) found, {added} PDF(s) added. "
                f"Current total: {len(data['pdfs'])} PDF(s)."
            )
        else:
            await update.effective_message.reply_text("Unsupported file. Send PDF or JPG ZIP.")

    except zipfile.BadZipFile:
        data["failed"] += 1
        await update.effective_message.reply_text("Invalid ZIP file. Please send a valid JPG ZIP.")
    except Exception as exc:
        log.exception("File processing failed")
        data["failed"] += 1
        await update.effective_message.reply_text(f"Could not process this file: {exc}")
    finally:
        if os.path.exists(source):
            os.remove(source)


async def done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = context.user_data
    if not data.get("folder"):
        await update.effective_message.reply_text("Please send /start first.")
        return

    pdfs = data["pdfs"]
    if not pdfs:
        await update.effective_message.reply_text(
            f"No PDFs ready. Duplicates skipped: {data['duplicates']}; "
            f"failed files: {data['failed']}. You can send more files."
        )
        return

    count = len(pdfs)
    archive_name = f"PDF_FILES_{count}.zip"
    archive_path = os.path.join(data["folder"], archive_name)

    try:
        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, path in pdfs:
                archive.write(path, arcname=name)

        # The count is shown in the ZIP filename and in Telegram's caption,
        # outside the ZIP archive itself.
        with open(archive_path, "rb") as f:
            await update.effective_message.reply_document(
                document=f,
                filename=archive_name,
                caption=(
                    f"{archive_name}  |  Total: {count} PDF files\n"
                    f"Duplicates skipped: {data['duplicates']}  |  "
                    f"Failed: {data['failed']}\n"
                    "Session remains active. You can send more files."
                ),
            )

        for _, path in pdfs:
            if os.path.exists(path):
                os.remove(path)

        data["pdfs"].clear()
        data["hashes"].clear()
        data["received"] = 0
        data["duplicates"] = 0
        data["failed"] = 0

    except Exception:
        log.exception("ZIP creation/upload failed")
        await update.effective_message.reply_text(
            "ZIP creation or upload failed. Your files are still in the session; try /done again."
        )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    folder = context.user_data.get("folder")
    if folder:
        shutil.rmtree(folder, ignore_errors=True)
    context.user_data.clear()
    await update.effective_message.reply_text(
        "Session ended and temporary files deleted. Send /start to begin again."
    )


tg = Application.builder().token(BOT_TOKEN).build()


@asynccontextmanager
async def lifespan(api):
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable missing.")

    tg.add_handler(CommandHandler("start", start))
    tg.add_handler(CommandHandler("done", done))
    tg.add_handler(CommandHandler("cancel", cancel))
    tg.add_handler(MessageHandler(filters.Document.ALL, receive))

    await tg.initialize()
    await tg.start()
    await tg.updater.start_polling(drop_pending_updates=False)
    log.info("PDF ZIP bot started")

    try:
        yield
    finally:
        await tg.updater.stop()
        await tg.stop()
        await tg.shutdown()


api = FastAPI(lifespan=lifespan)


@api.get("/")
async def home():
    return {"name": "PDF ZIP", "status": "healthy"}


@api.get("/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run(api, host="0.0.0.0", port=PORT)
