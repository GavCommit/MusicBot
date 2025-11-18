import os
import re
import aiohttp
import asyncio
import configparser
from aiogram import Bot, Dispatcher, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ParseMode
from aiogram.utils import executor
from bs4 import BeautifulSoup as bs

# Import config
config_path = "MusicBot.conf"  

if not os.path.exists(config_path):
    raise FileNotFoundError(f"Config file not found at {config_path}")
config = configparser.ConfigParser()
config.read(config_path)

TOKEN = config.get("Settings", "TOKEN")
FILE_SIZE_LIMIT = config.getint("Settings", "FILE_SIZE_LIMIT") * 1024 * 1024 # in Mbites
muzmo_baselink = "https://rmr.muzmo.cc"

bot = Bot(token=TOKEN)
dp = Dispatcher(bot)
user_data = {}


semaphore = asyncio.Semaphore(10)

# /start
@dp.message_handler(commands=["start"])
async def start(message: types.Message):
    user_id = message.from_user.id
    user_data[user_id] = {"songs": [], "links": []}
    greeting = f"Здравствуйте, {message.from_user.first_name}! Этот бот поможет вам найти и скачать музыку."
    await message.answer(greeting)
    await message.answer("Введите название песни или исполнителя:")

# Message handler (music search)
@dp.message_handler(content_types=types.ContentTypes.TEXT)
async def handle_text(message: types.Message):
    user_id = message.from_user.id
    query = message.text.strip()

    if user_id not in user_data:
        user_data[user_id] = {"songs": [], "links": []}

    await get_music(message, query)

# Async music parser
async def get_music(message: types.Message, query: str):
    user_id = message.from_user.id
    user_data[user_id] = {"songs": [], "links": []}

    search_query = query.replace(" ", "+")

    async with aiohttp.ClientSession() as session:
        url = f"{muzmo_baselink}/search?q={search_query}"
         
        try:
            async with semaphore, session.get(url) as response:
                html = await response.text()
                data = bs(html, "html.parser")
                names = data.findAll('a', class_="block")
                hrefs = [i['href'] for i in names if i['href'].startswith('/get_new?') or i['href'].startswith('/info?id')]
            for item in names:
                song_name = "".join(" ".join(item.text.split()).split(", 320Kb/s"))
                if song_name.endswith(')'):
                    user_data[user_id]["songs"].append(song_name)
            for link in hrefs:
                user_data[user_id]["links"].append(muzmo_baselink+link)
        except Exception as ex:
            print(f"[!] Ошибка парсинга: {ex}")
        if user_data[user_id]["songs"] and user_data[user_id]["links"]:
            markup = InlineKeyboardMarkup()
            for index, song in enumerate(user_data[user_id]["songs"]):
                songname = song.replace(")", "").split('(')[0].strip()
                button = InlineKeyboardButton(songname, callback_data=f"{user_id}:{index}")
                markup.add(button)
                url = url.replace(')','\)' )
            await message.answer(f"Музыка найдена на сайте [Muzmo]({muzmo_baselink})\.  [На сайт]({url})\.", parse_mode=ParseMode.MARKDOWN_V2, reply_markup=markup)
            return
        url = url.replace(')','\)' )
        await message.answer(f"К сожалению, ничего не найдено\. [Посмотреть на сайте]({ url })\.", parse_mode=ParseMode.MARKDOWN_V2)

# Button handler
@dp.callback_query_handler(lambda callback: True)
async def callback(callback: types.CallbackQuery):
    user_id, index = map(int, callback.data.split(":"))
    user_songs = user_data.get(user_id, {}).get("songs", [])
    user_links = user_data.get(user_id, {}).get("links", [])

    if not user_songs or not user_links:
        await callback.message.answer("Ошибка: данные о песнях отсутствуют. Попробуйте начать заново.")
        return

    song_name = user_songs[index]
    filename = song_name.replace(")", "").split('(')[0].replace(" ", "_") + ".mp3"
    link = user_links[index]
    
    await download(callback.message, filename, link)

# Downloading
async def download(message: types.Message, filename: str, link: str):
    try:
        download_url = await get_downloadlink(link)
        if not download_url:
            download_url = await get_downloadlink(link)
            if not download_url:
                await message.answer("Не удалось получить ссылку на скачивание.")
                return

        
        
        async with aiohttp.ClientSession() as session:
            async with semaphore, session.get(download_url) as response:
                file_size = int(response.headers.get("Content-Length", 0))

                if file_size > FILE_SIZE_LIMIT:
                    await message.answer(f"Файл слишком большой ({file_size / 1024 / 1024:.2f} MB). Лимит: {FILE_SIZE_LIMIT / 1024 / 1024} MB.")
                    return


                await bot.send_chat_action(message.chat.id, "record_voice")
                with open(filename, 'wb') as f:
                    async for chunk in response.content.iter_chunked(512):
                        f.write(chunk)

                with open(filename, 'rb') as audio_file:
                    await bot.send_chat_action(message.chat.id, 'upload_document')
                    await bot.send_audio(message.chat.id, audio_file)

        os.remove(filename)

    except Exception as ex:
        print(f"[!] (download) Ошибка загрузки: {ex}")
        await message.answer("Ошибка при загрузке. Попробуйте снова.")

# Get download link
async def get_downloadlink(link: str) -> str:
    async with aiohttp.ClientSession() as session:
        try:
            href = None
            while not href:    
                async with semaphore, session.get(link) as response:
                    
                    html = await response.text()
                   
                    data = bs(html, 'html.parser')
                                        
                    name = data.findAll('a', class_='block')
                    
                    if name:
                        href = [i['href'] for i in name if i['href'].startswith('/get/music')][0]

                        if not href:
                            name = data.findAll('div', class_='mzmlght')[1]
                            href = name.find("input", {'name' : "input"}).get("value")

                        if href:
                            return muzmo_baselink+href


        except Exception as ex:
            print(f"[!] (get_downloadlink) Ошибка получения ссылки: {ex}")

    return None

# Bot startup
if __name__ == "__main__":
    
    executor.start_polling(dp, skip_updates=True)