import os
import sqlite3
import asyncio
import time
from datetime import datetime
from urllib.parse import urljoin

import requests
from dotenv import load_dotenv
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from telegram import Bot
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# =======================
# Настройки и секреты
# =======================
load_dotenv("secrets.env")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = int(os.getenv("TELEGRAM_CHANNEL_ID"))
ADMIN_ID = int(os.getenv("ADMIN_ID"))
POST_INTERVAL = min(int(os.getenv("POST_INTERVAL", 600)), 1200)  # максимум 20 минут

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "mistralai/Mistral-Nemo-Instruct-2407")

ACCOUNTS = ["TechCrunch", "PopCrave", "BusinessInsider"]
BASE_URL = "https://x.com/"
DB_FILE = "x_posts.db"

# =======================
# Инициализация базы
# =======================
def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account TEXT,
            post_id TEXT UNIQUE,
            text TEXT,
            status TEXT DEFAULT 'new',
            created_at TEXT
        )
    """)
    conn.commit()
    conn.close()

# =======================
# Сбор постов с профилей X через Selenium
# =======================
def fetch_posts(account: str):
    chrome_options = Options()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--disable-gpu")
    service = Service()
    driver = webdriver.Chrome(service=service, options=chrome_options)

    url = urljoin(BASE_URL, account)
    driver.get(url)
    driver.implicitly_wait(5)

    posts = set()
    try:
        elements = driver.find_elements(By.CSS_SELECTOR, "article a[href*='/status/']")
        for elem in elements:
            href = elem.get_attribute("href")
            if href:
                post_id = href.split("/")[-1]
                text_elem = elem.find_element(By.XPATH, "./ancestor::article//div[@data-testid='tweetText']")
                text = text_elem.text if text_elem else ""
                if text.strip():
                    posts.add((post_id, text.strip()))
    except Exception as e:
        print(f"Ошибка при сборе постов {account}: {e}")
    finally:
        driver.quit()

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return [{"id": pid, "text": txt, "created_at": now} for pid, txt in posts]

# =======================
# Сохранение постов в БД
# =======================
def save_posts_to_db(posts, account):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    for post in posts:
        cursor.execute(
            "INSERT OR IGNORE INTO posts (account, post_id, text, created_at) VALUES (?, ?, ?, ?)",
            (account, post["id"], post["text"], post["created_at"])
        )
    conn.commit()
    conn.close()

# =======================
# Получить следующий пост
# =======================
def get_next_post():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT id, text FROM posts WHERE status='new' ORDER BY id ASC LIMIT 1")
    row = cursor.fetchone()
    conn.close()
    return row

# =======================
# Обновить статус поста
# =======================
def mark_post_sent(post_id):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("UPDATE posts SET status='sent' WHERE id=?", (post_id,))
    conn.commit()
    conn.close()

# =======================
# Перевод текста через OpenRouter
# =======================
def translate_text(text):
    prompt = f"Переведи следующий текст на русский для Telegram-канала, сохрани смысл и цифры, без добавлений:\n\n{text}"
    payload = {
        "model": OPENROUTER_MODEL,
        "messages": [
            {"role": "system", "content": "Ты профессиональный переводчик."},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.3,
        "max_tokens": 2000
    }
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json"
    }
    try:
        r = requests.post("https://openrouter.ai/api/v1/chat/completions", json=payload, headers=headers, timeout=60)
        r.raise_for_status()
        data = r.json()
        return data["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"Ошибка перевода: {e}")
        return text

# =======================
# Отправка поста в Telegram
# =======================
async def send_next_post(context: ContextTypes.DEFAULT_TYPE):
    post = get_next_post()
    if not post:
        print("Нет новых постов для отправки")
        return

    post_id, text = post
    translated = translate_text(text)

    try:
        await context.bot.send_message(chat_id=CHAT_ID, text=translated)
        mark_post_sent(post_id)
        print(f"✅ Отправлен пост ID {post_id}")
    except Exception as e:
        print(f"Ошибка отправки в Telegram: {e}")

# =======================
# Цикл отправки с интервалом
# =======================
async def start_scheduled_posting(context: ContextTypes.DEFAULT_TYPE):
    while True:
        await send_next_post(context)
        await asyncio.sleep(POST_INTERVAL)

# =======================
# Команда /start
# =======================
async def start(update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text("⚡ Бот запущен. Публикации идут по расписанию.")
    context.application.create_task(start_scheduled_posting(context))

# =======================
# Основная функция
# =======================
def main():
    init_db()

    # Сбор новых постов
    for account in ACCOUNTS:
        posts = fetch_posts(account)
        save_posts_to_db(posts, account)

    application = ApplicationBuilder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    print("Бот запущен")
    application.run_polling()

if __name__ == "__main__":
    main()
