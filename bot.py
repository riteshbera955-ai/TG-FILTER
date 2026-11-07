import os, re, shutil, tempfile, zipfile, logging
from contextlib import asynccontextmanager
import fitz, uvicorn
from fastapi import FastAPI
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

TOKEN=os.getenv("BOT_TOKEN","").strip()
PORT=int(os.getenv("PORT","10000"))
MAX_MB=20
MAX_FILES=100
PATTERN=re.compile(r"(?<!\d)(?:\d[\s-]*){11}\d(?!\d)")
logging.basicConfig(level=logging.INFO)
log=logging.getLogger("pdf-name")

def find_number(path):
    doc=fitz.open(path)
    try: text="\n".join(p.get_text("text") for p in doc)
    finally: doc.close()
    for m in PATTERN.finditer(text):
        n=re.sub(r"\D","",m.group())
        if len(n)==12 and len(set(n))>1: return n
    return None

def to_jpg(path, folder, stem):
    doc=fitz.open(path); result=[]
    try:
        multi=len(doc)>1
        for i,page in enumerate(doc,1):
            pix=page.get_pixmap(matrix=fitz.Matrix(200/72,200/72),alpha=False,colorspace=fitz.csRGB)
            name=f"{stem}_page{i}.jpg" if multi else f"{stem}.jpg"
            out=os.path.join(folder,name)
            pix.save(out,jpg_quality=90)
            result.append(out)
    except Exception:
        for p in result:
            if os.path.exists(p): os.remove(p)
        raise
    finally: doc.close()
    return result

async def start(update:Update, context:ContextTypes.DEFAULT_TYPE):
    old=context.user_data.get("folder")
    if old: shutil.rmtree(old,ignore_errors=True)
    context.user_data["folder"]=tempfile.mkdtemp(prefix="pdfname_")
    context.user_data["jpgs"]=[]
    context.user_data["seen"]=set()
    context.user_data["dupes"]=0
    context.user_data["failed"]=0
    await update.effective_message.reply_text(
        "Welcome to PDF NAME! Send PDF files one by one (max 20 MB each, up to 100 unique PDFs). "
        "Exact duplicate 12-digit matches are skipped. Every page of each accepted PDF becomes JPG. "
        "Send /done for a ZIP of JPGs or /cancel to discard. Scanned PDFs may not be detected."
    )

async def receive(update:Update, context:ContextTypes.DEFAULT_TYPE):
    folder=context.user_data.get("folder")
    if not folder:
        await update.effective_message.reply_text("Send /start first.")
        return
    document=update.effective_message.document
    if not document or (document.mime_type!="application/pdf" and not (document.file_name or "").lower().endswith(".pdf")):
        await update.effective_message.reply_text("Please send a PDF document.")
        return
    if document.file_size and document.file_size>MAX_MB*1024*1024:
        await update.effective_message.reply_text(f"PDF exceeds {MAX_MB} MB.")
        return
    if len(context.user_data.get("jpgs",[]))>=MAX_FILES:
        await update.effective_message.reply_text(f"Batch limit is {MAX_FILES} unique PDFs. Send /done.")
        return
    src=os.path.join(folder,str(document.file_unique_id)+".pdf")
    try:
        f=await context.bot.get_file(document.file_id)
        await f.download_to_drive(src)
        number=find_number(src)
        if not number:
            context.user_data["failed"]+=1
            os.remove(src)
            await update.effective_message.reply_text("No readable 12-digit number found. Scanned/image-only PDFs need OCR.")
            return
        if number in context.user_data["seen"]:
            context.user_data["dupes"]+=1
            os.remove(src)
            await update.effective_message.reply_text("Duplicate match — PDF skipped.")
            return
        stem="PDF_XXXX"+number[-4:]
        made=to_jpg(src,folder,stem)
        context.user_data["seen"].add(number)
        context.user_data["jpgs"].extend(made)
        os.remove(src)
        await update.effective_message.reply_text(f"Converted to {len(made)} JPG image(s).")
    except Exception:
        log.exception("PDF conversion failed")
        if os.path.exists(src): os.remove(src)
        await update.effective_message.reply_text("Could not process this PDF. Please check the file and try again.")

async def done(update:Update, context:ContextTypes.DEFAULT_TYPE):
    folder=context.user_data.get("folder")
    jpgs=context.user_data.get("jpgs",[])
    if not folder or not jpgs:
        await update.effective_message.reply_text("No JPG images ready. Send /start first.")
        return
    archive=os.path.join(folder,"PDF_NAME_JPG_images.zip")
    try:
        with zipfile.ZipFile(archive,"w",zipfile.ZIP_DEFLATED) as z:
            for p in jpgs: z.write(p,arcname=os.path.basename(p))
        with open(archive,"rb") as f:
            await update.effective_message.reply_document(
                document=f,filename="PDF_NAME_JPG_images.zip",
                caption=f"Done: {len(jpgs)} JPG image(s). Duplicate PDFs skipped: {context.user_data.get('dupes',0)}. Unreadable/no-match: {context.user_data.get('failed',0)}."
            )
    finally:
        shutil.rmtree(folder,ignore_errors=True)
        for k in ("folder","jpgs","seen","dupes","failed"): context.user_data.pop(k,None)

async def cancel(update:Update, context:ContextTypes.DEFAULT_TYPE):
    folder=context.user_data.pop("folder",None)
    if folder: shutil.rmtree(folder,ignore_errors=True)
    for k in ("jpgs","seen","dupes","failed"): context.user_data.pop(k,None)
    await update.effective_message.reply_text("Batch cancelled; temporary files deleted.")

tg=Application.builder().token(TOKEN).build()
@asynccontextmanager
async def lifespan(api):
    if not TOKEN: raise RuntimeError("BOT_TOKEN environment variable is missing.")
    tg.add_handler(CommandHandler("start",start))
    tg.add_handler(CommandHandler("done",done))
    tg.add_handler(CommandHandler("cancel",cancel))
    tg.add_handler(MessageHandler(filters.Document.ALL,receive))
    await tg.initialize(); await tg.start(); await tg.updater.start_polling(drop_pending_updates=True)
    log.info("PDF NAME Telegram bot started")
    try: yield
    finally:
        await tg.updater.stop(); await tg.stop(); await tg.shutdown()

api=FastAPI(lifespan=lifespan)
@api.get("/")
async def home(): return {"name":"PDF NAME","status":"healthy","output":"JPG"}
@api.get("/health")
async def health(): return {"status":"ok"}
if __name__=="__main__": uvicorn.run(api,host="0.0.0.0",port=PORT)
