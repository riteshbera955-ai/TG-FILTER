import os
import re
import cv2
import numpy as np
from pdf2image import convert_from_path
from PIL import Image, ImageEnhance, ImageFilter
import pytesseract
from telegram import Update
from telegram.ext import Application, MessageHandler, filters
import hashlib

# --- CONFIG ---
TELEGRAM_BOT_TOKEN = '8681990378:AAFbGpzsTXc7dBsl02aQQxk7iqsR64kCRz8'  # ⚠️ নিজের বট টোকেন দাও
DATA_DIR = 'pdfs/'
TEMP_DIR = 'temp/'
DUPE_LOG = 'seen_aadhaar.txt'  # আধার নম্বর লগ করবে
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(TEMP_DIR, exist_ok=True)

# --- আধার নম্বর খুঁজার প্যাটার্ন ---
AADHAAR_PATTERN = re.compile(r'\b(\d{4}\s?\d{4}\s?\d{4})\b')

# --- OCR প্রসেসিং ---
def preprocess_image(img):
    img = img.convert('L')  # গ্রেস্কেল
    enhancer = ImageEnhance.Contrast(img)
    img = enhancer.enhance(2)
    img = img.filter(ImageFilter.MedianFilter())
    return img

def extract_text_from_pdf(pdf_path):
    try:
        pages = convert_from_path(pdf_path, dpi=300)
        text = ""
        for page in pages:
            page = preprocess_image(page)
            page_cv = np.array(page)
            _, binary = cv2.threshold(page_cv, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            text += pytesseract.image_to_string(binary, lang='eng')
        return text
    except Exception as e:
        print(f"OCR Error: {e}")
        return ""

# --- আধার নম্বর এক্সট্রাক্ট ---
def extract_aadhaar(text):
    matches = AADHAAR_PATTERN.findall(text)
    cleaned = [re.sub(r'\s+', '', m) for m in matches]
    # ফিল্টার: যদি 12 ডিজিট না হয়
    valid = [a for a in cleaned if len(a) == 12 and a.isdigit()]
    return valid[0] if valid else None

# --- ডুপ চেক ---
def is_duplicate(aadhaar):
    if not os.path.exists(DUPE_LOG):
        return False
    with open(DUPE_LOG, 'r') as f:
        return aadhaar in f.read()

def log_aadhaar(aadhaar):
    with open(DUPE_LOG, 'a') as f:
        f.write(aadhaar + '\n')

# --- ফাইল রিনেম ও সেভ ---
def rename_and_save(pdf_path, aadhaar):
    new_name = f"{aadhaar}.pdf"
    new_path = os.path.join(DATA_DIR, new_name)
    if not os.path.exists(new_path):
        os.rename(pdf_path, new_path)
        return new_path
    return None

# --- হ্যান্ডলার ---
async def handle_document(update: Update, context):
    message = update.message
    user_id = message.from_user.id

    if not message.document or not message.document.file_name.endswith('.pdf'):
        await message.reply_text("❌ শুধুমাত্র PDF ফাইল পাঠাও।")
        return

    file_name = message.document.file_name
    temp_pdf = os.path.join(TEMP_DIR, f"{user_id}_{hashlib.md5(str(message.message_id).encode()).hexdigest()}.pdf")

    try:
        # ডাউনলোড
        file = await message.document.get_file()
        await file.download_to_drive(temp_pdf)

        # OCR
        text = extract_text_from_pdf(temp_pdf)
        if not text:
            await message.reply_text("❌ OCR ব্যর্থ। ফাইলটি স্পষ্ট কিনা চেক করো।")
            os.remove(temp_pdf)
            return

        aadhaar = extract_aadhaar(text)
        if not aadhaar:
            await message.reply_text("❌ আধার নম্বর পাওয়া যায়নি।")
            os.remove(temp_pdf)
            return

        if is_duplicate(aadhaar):
            await message.reply_text(f"⚠️ ডুপ্লিকেট আধার: `{aadhaar}`", parse_mode='Markdown')
            os.remove(temp_pdf)
            return

        # রিনেম ও সেভ
        final_path = rename_and_save(temp_pdf, aadhaar)
        if final_path:
            log_aadhaar(aadhaar)
            await message.reply_text(f"✅ সফল! আধার: `{aadhaar}`\n📁 সংরক্ষিত হিসাবে: `{aadhaar}.pdf`", parse_mode='Markdown')
        else:
            await message.reply_text(f"📁 ফাইল ইতিমধ্যে আছে: `{aadhaar}.pdf`")

    except Exception as e:
        await message.reply_text(f"❌ ত্রুটি: {str(e)}")
        if os.path.exists(temp_pdf):
            os.remove(temp_pdf)

# --- MAIN ---
if __name__ == '__main__':
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    print("🚀 বট চালু হয়েছে... আধার PDF পাঠাও।")
    app.run_polling()
