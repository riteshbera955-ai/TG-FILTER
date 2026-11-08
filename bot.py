import asyncio, hashlib, logging, os, shutil, tempfile, zipfile
from contextlib import asynccontextmanager

import fitz
import uvicorn
from fastapi import FastAPI
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

BOT_TOKEN=os.getenv("BOT_TOKEN","").strip()
PORT=int(os.getenv("PORT","10000"))
MAX_PDF_MB=20
MAX_PDFS_PER_BATCH=100
DPI=200
QUALITY=90

logging.basicConfig(level=logging.INFO)
log=logging.getLogger("pdf-name")

def sha256_file(path):
    h=hashlib.sha256()
    with open(path,"rb") as f:
        for chunk in iter(lambda:f.read(1024*1024),b""):
            h.update(chunk)
    return h.hexdigest()

def convert_to_jpg(pdf_path, folder, stem):
    doc=fitz.open(pdf_path)
    created=[]
    try:
        matrix=fitz.Matrix(DPI/72,DPI/72)
        multi=len(doc)>1
        for page_no,page in enumerate(doc,1):
            pix=page.get_pixmap(matrix=matrix,alpha=False,colorspace=fitz.csRGB)
            name=f"{stem}_page{page_no}.jpg" if multi else f"{stem}.jpg"
            out=os.path.join(folder,name)
            pix.save(out,jpg_quality=QUALITY)
            created.append(out)
    except Exception:
        for p in created:
            if os.path.exists(p): os.remove(p)
        raise
    finally:
        doc.close()
    return created

async def worker(app, user_id):
    data=app.user_data[user_id]
    q=data["queue"]
    while True:
        item=await q.get()
        try:
            if item is None: return
            source,seq,original=item
            try:
                jpgs=await asyncio.to_thread(convert_to_jpg,source,data["folder"],f"PDF_{seq:03d}")
                data["jpgs"].extend(jpgs)
                await app.bot.send_message(user_id,f"Processed PDF {seq}/{MAX_PDFS_PER_BATCH}: {len(jpgs)} JPG page(s).")
            except Exception:
                log.exception("Conversion failed")
                data["failed"]+=1
                await app.bot.send_message(user_id,f"Could not convert: {original}")
            finally:
                if os.path.exists(source): os.remove(source)
        finally:
            q.task_done()

async def start(update:Update, context:ContextTypes.DEFAULT_TYPE):
    # /start begins or resets a session. /done does not end it.
    old=context.user_data.get("folder")
    if old: shutil.rmtree(old,ignore_errors=True)
    context.user_data.clear()
    folder=tempfile.mkdtemp(prefix=f"pdfname_{update.effective_user.id}_")
    context.user_data.update(folder=folder,queue=asyncio.Queue(),jpgs=[],
        hashes=set(),pdf_count=0,duplicates=0,failed=0,lock=asyncio.Lock())
    context.user_data["worker"]=asyncio.create_task(worker(context.application,update.effective_user.id))
    await update.effective_message.reply_text(
        "PDF NAME is ready. Send up to 100 PDFs in this batch (20 MB each).\n"
        "Every page is converted to JPG. Exact duplicate files are skipped.\n"
        "Send /done anytime to receive the current JPG ZIP. The session stays open, "
        "so you can send more PDFs after /done without /start.\n"
        "Send /cancel to end and delete the current session."
    )

async def receive(update:Update,context:ContextTypes.DEFAULT_TYPE):
    d=context.user_data
    if not d.get("folder"):
        await update.effective_message.reply_text("Send /start once to begin.")
        return
    doc=update.effective_message.document
    if not doc or (doc.mime_type!="application/pdf" and not (doc.file_name or "").lower().endswith(".pdf")):
        await update.effective_message.reply_text("Please send a PDF as a document.")
        return
    if doc.file_size and doc.file_size>MAX_PDF_MB*1024*1024:
        await update.effective_message.reply_text(f"PDF exceeds {MAX_PDF_MB} MB.")
        return
    async with d["lock"]:
        if d["pdf_count"]>=MAX_PDFS_PER_BATCH:
            await update.effective_message.reply_text("100-PDF batch limit reached. Send /done for ZIP; then /start for a fresh batch.")
            return
        d["pdf_count"]+=1
        seq=d["pdf_count"]
    source=os.path.join(d["folder"],f"input_{seq:03d}.pdf")
    try:
        tgfile=await context.bot.get_file(doc.file_id)
        await tgfile.download_to_drive(source)
        fingerprint=await asyncio.to_thread(sha256_file,source)
        async with d["lock"]:
            if fingerprint in d["hashes"]:
                d["duplicates"]+=1
                os.remove(source)
                await update.effective_message.reply_text("Exact duplicate PDF file skipped.")
                return
            d["hashes"].add(fingerprint)
            await d["queue"].put((source,seq,doc.file_name or f"PDF {seq}"))
        await update.effective_message.reply_text(f"Received PDF {seq}/{MAX_PDFS_PER_BATCH}; added to processing queue.")
    except Exception:
        log.exception("Receive failed")
        if os.path.exists(source): os.remove(source)
        d["failed"]+=1
        await update.effective_message.reply_text("Could not receive this PDF. Please try again.")

async def done(update:Update,context:ContextTypes.DEFAULT_TYPE):
    d=context.user_data
    if not d.get("folder"):
        await update.effective_message.reply_text("Send /start once to begin.")
        return
    await update.effective_message.reply_text("Finishing queued PDFs and preparing your JPG ZIP. Please wait…")
    await d["queue"].join()
    jpgs=d["jpgs"]
    if not jpgs:
        await update.effective_message.reply_text(
            f"No new JPGs in this batch. Exact duplicates: {d['duplicates']}; failed: {d['failed']}. "
            "You can send more PDFs now; no need to /start again."
        )
        return
    archive=os.path.join(d["folder"],"PDF_NAME_JPG_images.zip")
    try:
        with zipfile.ZipFile(archive,"w",zipfile.ZIP_DEFLATED) as z:
            for p in jpgs: z.write(p,arcname=os.path.basename(p))
        with open(archive,"rb") as f:
            await update.effective_message.reply_document(
                document=f,filename="PDF_NAME_JPG_images.zip",
                caption=f"ZIP ready: {len(jpgs)} JPG(s). Exact duplicate files skipped: {d['duplicates']}. Failed: {d['failed']}.\nSession remains active — send more PDFs, then /done again."
            )
        # Clear only completed batch outputs/counters; keep session and worker alive.
        for p in jpgs:
            if os.path.exists(p): os.remove(p)
        d["jpgs"].clear()
        d["hashes"].clear()
        d["pdf_count"]=0
        d["duplicates"]=0
        d["failed"]=0
        await update.effective_message.reply_text("Ready for the next batch of up to 100 PDFs. Send more PDFs whenever you like.")
    except Exception:
        log.exception("ZIP creation/upload failed")
        await update.effective_message.reply_text("ZIP creation or upload failed. Your session is still active; try /done again.")

async def cancel(update:Update,context:ContextTypes.DEFAULT_TYPE):
    d=context.user_data
    worker_task=d.get("worker")
    if worker_task and not worker_task.done(): worker_task.cancel()
    folder=d.get("folder")
    if folder: shutil.rmtree(folder,ignore_errors=True)
    context.user_data.clear()
    await update.effective_message.reply_text("Session ended; temporary files deleted. Send /start to begin again.")

tg=Application.builder().token(BOT_TOKEN).build()
@asynccontextmanager
async def lifespan(api):
    if not BOT_TOKEN: raise RuntimeError("BOT_TOKEN environment variable missing.")
    tg.add_handler(CommandHandler("start",start))
    tg.add_handler(CommandHandler("done",done))
    tg.add_handler(CommandHandler("cancel",cancel))
    tg.add_handler(MessageHandler(filters.Document.ALL,receive))
    await tg.initialize(); await tg.start(); await tg.updater.start_polling(drop_pending_updates=False)
    log.info("PDF NAME bot started")
    try: yield
    finally:
        await tg.updater.stop(); await tg.stop(); await tg.shutdown()

api=FastAPI(lifespan=lifespan)
@api.get("/")
async def home(): return {"name":"PDF NAME","status":"healthy"}
@api.get("/health")
async def health(): return {"status":"ok"}
if __name__=="__main__": uvicorn.run(api,host="0.0.0.0",port=PORT)
