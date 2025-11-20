import os, re
import aiohttp
import asyncio
import configparser
import difflib
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile, URLInputFile
from bs4 import BeautifulSoup as bs
#import logging

# Import config
config_path = "MusicBot.conf"  

if not os.path.exists(config_path):
    raise FileNotFoundError(f"Config file not found at {config_path}")
config = configparser.ConfigParser()
config.read(config_path)

TOKEN = config.get("Settings", "TOKEN")
FILE_SIZE_LIMIT = config.getint("Settings", "FILE_SIZE_LIMIT") * 1024 * 1024 # in Mbites
PAGES_SCANNING = config.getint("Settings", "PAGES_SCANNING")
SEARCH_RESULTS = config.getint("Settings", "SEARCH_RESULTS")
base_url = "https://rmr.muzmo.cc"

bot = Bot(token=TOKEN)
dp = Dispatcher()
#logging.basicConfig(level=logging.INFO)
#logger = logging.getLogger(__name__)

semaphore = asyncio.Semaphore(10)

# /start
@dp.message(F.chat.type == "private", Command(commands=['start','старт','от_винта']))
async def start(message: Message):
    greeting = f"Здравствуйте, {message.from_user.first_name}! Этот бот поможет вам найти и скачать музыку."
    await message.answer(greeting)
    await message.answer("Введите название песни или исполнителя:")

# Message handler (music search)
@dp.message()
async def handle_text(message: Message):
    query = message.text.strip().replace(" ", "+")

    if len(query) < 3:
        await message.answer("Запрос для поиска не менее 3х символов")
        return

    music_data = await get_music(query=query, pages=PAGES_SCANNING) # делаем запросы к сайту (асинхрон, несколько страниц)

    music_data_filtered = await top_songs(music_data=music_data, query=query, top_count=SEARCH_RESULTS) # фильтруем результат поиска, находим наибольшее совпадение

    await send_downloading_kb(message=message, url = f"/search?q={query}", music_data_filtered=music_data_filtered) #отправка клавиатуры с песнями


# Async music parser
async def get_music(query: str, pages: int = 3) -> list:
    async with aiohttp.ClientSession() as session:
        tasks = [
            session.get(f"{base_url}/search?q={query}&start={page*15}", timeout=10)
            for page in range(pages)
        ]
        responses = await asyncio.gather(*tasks, return_exceptions=True)
        all_music_data = []
        
        for response in responses:
            if isinstance(response, Exception) or not hasattr(response, 'status'):
                continue
            
            if response.status == 200:
                html = await response.text()
                soup = bs(html, "html.parser")
                
                for item in soup.find_all('a', class_="block"):
                    href = item.get('href', '')
                    if href.startswith(('/info?id')):   # '/get_new?',  но не всегда скачивается  НЕ ДОБАВЛЯТЬ СЛОМАЕТ CALLBACK
                        text = item.get_text(strip=True)
                        if " - " in text and "(" in text:
                            try:
                                name = text.split('(')[0].strip()
                                time = text.split('(')[1].split(',')[0].strip()
                                all_music_data.append((
                                    f"{name}({time})",
                                    f"{href[9:]}" # {base_url} получаем просто id песни
                                ))
                            except IndexError:
                                continue
            
        return all_music_data

#song filter
async def top_songs(music_data, query, top_count=10):
    if len(music_data) <= top_count:
        return music_data
    
    query_lower = query.lower()
    def process_chunk(chunk):
        chunk_scores = []
        for song in chunk:
            song_lower = song[0].lower()
            
            # Используем partial_ratio для неполных совпадений
            similarity = difflib.SequenceMatcher(
                None, query_lower, song_lower
            ).ratio()
            
            # Дополнительные метрики для точности
            partial_similarity = difflib.SequenceMatcher(
                None, query_lower, song_lower
            ).quick_ratio()
            
            bonus = 0
            if query_lower in song_lower:
                bonus = 40  # Точное вхождение
            elif any(word in song_lower for word in query_lower.split()):
                bonus = 20  # Хотя бы одно слово
            
            # total score
            score = (similarity * 50 + partial_similarity * 30 + bonus)
            chunk_scores.append((score, song))
        
        return chunk_scores
    
    # Параллельная обработка
    chunk_size = max(1, len(music_data) // 4)
    chunks = [music_data[i:i + chunk_size] for i in range(0, len(music_data), chunk_size)]
    
    loop = asyncio.get_event_loop()
    tasks = [loop.run_in_executor(None, process_chunk, chunk) for chunk in chunks]
    chunk_results = await asyncio.gather(*tasks)
    
    all_scores = []
    for chunk_scores in chunk_results:
        all_scores.extend(chunk_scores)
    
    all_scores.sort(key=lambda x: x[0], reverse=True)
    return [song for _, song in all_scores[:top_count]]


async def send_downloading_kb(message, url:str = base_url, music_data_filtered: list = []):
    if not music_data_filtered: # если musiс_data пустая
        await message.answer(f'К сожалению, ничего не найдено. <a href="{base_url+url}">Посмотреть на сайте</a>.', parse_mode="HTML")
        return 

    buttons = []
    for song, id in music_data_filtered:
        buttons.append(
            [InlineKeyboardButton(
            text=song,
            callback_data=id
            )]
            )
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await message.answer(
        f'Музыка найдена на сайте <a href="{base_url}">Muzmo</a>.  <a href="{base_url+url}">На сайт</a>.',
        parse_mode="HTML",
        reply_markup=kb
        )

# Button handler
@dp.callback_query()
async def download_song(callback: CallbackQuery):
    id = callback.data
    link = base_url + "/info?id=" + id

    song = "не_найдена"
    for row in callback.message.reply_markup.inline_keyboard:
        for button in row:
            if button.callback_data == id:
                song = button.text   

    pattern = r'\s*\(\d+:\d+\)\s*$'
    song_without_timer = re.sub(pattern, "", song).strip()
    filename = song_without_timer.replace(" ", "_").replace("/", "_") + ".mp3" 
    await download(callback=callback, filename=filename, link=link)
    await callback.answer()


# Downloading
async def download(callback, filename: str, link: str):
    try:
        download_url = await get_downloadlink(link)
        if not download_url:
            download_url = await get_downloadlink(link)
            if not download_url:
                await callback.answer("Не удалось получить ссылку на скачивание.", show_alert=True)
                return

        async with aiohttp.ClientSession() as session:
            async with semaphore, session.get(download_url) as response:
                file_size = int(response.headers.get("Content-Length", 0))

                if file_size > FILE_SIZE_LIMIT:
                    await callback.answer(f"Файл слишком большой ({file_size / 1024 / 1024:.2f} MB). Лимит: {FILE_SIZE_LIMIT / 1024 / 1024} MB.", show_alert=True)
                    return

                performer = filename.split("_-_")[0].strip("_").replace("_", " ")
                title = filename.split("_-_")[1].strip("_").split(".mp3")[0].strip("_").replace("_", " ")
                if file_size < 20 * 1024 * 1024:
                    await bot.send_chat_action(callback.message.chat.id, 'upload_document')
                    audio_file = URLInputFile(
                        url=download_url,
                        filename=filename
                    )
                    await callback.message.answer_audio(audio_file, title=title, performer=performer)
                    return

                await bot.send_chat_action(callback.message.chat.id, "record_voice")
                with open(filename, 'wb') as f:
                    async for chunk in response.content.iter_chunked(512):
                        f.write(chunk)

                await bot.send_chat_action(callback.message.chat.id, 'upload_document')

                audio_file = FSInputFile(filename, filename=filename)
                await callback.message.answer_audio(audio_file, title=title, performer=performer) # title='название', performer='исполнитель' 

        os.remove(filename)
        return

    except Exception as ex:
        print(f"[!] (download) Ошибка загрузки: {ex}")
        await callback.answer("Ошибка при загрузке. Попробуйте снова.", show_alert=True)

# Get download link
async def get_downloadlink(link: str) -> str: 
    async with aiohttp.ClientSession() as session:
        try:
            href = None
            while not href:    
                async with semaphore, session.get(link) as response:
                    html = await response.text()
                    data = bs(html, 'html.parser')
                    name = data.find_all('a', class_='block')
                    if name:
                        href = [i['href'] for i in name if i['href'].startswith('/get/music')][0]
                        if not href:
                            name = data.find_all('div', class_='mzmlght')[1]
                            href = name.find("input", {'name' : "input"}).get("value")
                        if href:
                            return base_url+href


        except Exception as ex:
            print(f"[!] (get_downloadlink) Ошибка получения ссылки: {ex}")

    return None

async def main():
    
    await dp.start_polling(bot)

# Bot startup
if __name__ == "__main__":
    print("Bot starts polling...")
    asyncio.run(main())
