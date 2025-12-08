import os, re, json
import aiohttp
import asyncio
import configparser
import difflib
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile, URLInputFile
from bs4 import BeautifulSoup as bs
import logging

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
sites = {
    "hitmo": {
        "code_letter": "b",
        "base_url": "https://rus.hitmotop.com",
        "base_download_url": "https://rus.hitmotop.com/get/music/"
    },
    "muzmo":{
    "code_letter": "a",
        "base_url": "https://rmr.muzmo.cc",
        "base_download_url": False
    }
}


bot = Bot(token=TOKEN)
dp = Dispatcher()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

semaphore = asyncio.Semaphore(10)

# /start
@dp.message(F.chat.type == "private", Command(commands=['start','старт','отвинта']))
async def start(message: Message):
    greeting = f"Здравствуйте, {message.from_user.first_name}! Этот бот поможет вам найти и скачать музыку."
    await message.answer(greeting)
    await message.answer("Введите название песни или исполнителя:")

# Message handler (music search)
@dp.message(F.text)
async def handle_text(message: Message):
    query = message.text
    if len(query) < 3:
        await message.answer("Запрос для поиска не менее 3х символов")
        return

    site, music_data = await get_music(query=query) # парсит пока не найдет
    if music_data:
        music_data_filtered = await top_songs(music_data=music_data, query=query, top_count=SEARCH_RESULTS) # фильтруем результат поиска, находим наибольшее совпадение
        music_data_filtered.insert(0, site) # добавляем флаг сайта к списку песен
    else:
        music_data_filtered = []
    await send_downloading_kb(message=message, url = f"/search?q={query}", music_data=music_data_filtered) #отправка клавиатуры с песнями

# Ищет на 2 сайтах и возвращает список песен  (Автор - Песня(время:время), ссылка)
async def get_music(query:str):
    music_data = await search_music_hitmo(query=query) # делаем запросы к сайту hitmo
    if music_data:
        return sites['hitmo']['code_letter'], music_data
    
    music_data = await search_music_muzmo(query=query, pages=PAGES_SCANNING) # делаем запросы к сайту muzmo (асинхрон, несколько страниц)
    if music_data:
        print(query)
        return sites['muzmo']['code_letter'], music_data
    return '', []
    
# Async music parser muzmo
async def search_music_muzmo(query: str, pages: int = 3) -> list:
    query = query.strip().replace(" ", "+")
    async with aiohttp.ClientSession() as session:
        tasks = [
            session.get(f"{sites['muzmo']['base_url']}/search?q={query}&start={page*15}", timeout=10)
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

#Async music parser hitmo
async def search_music_hitmo(query: str, limit: int = 40) -> list:
    query = query.strip().replace(" ", '-')
    url = f"https://rus.hitmotop.com/search?q={query}"
    
    songs_data = []
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as response:
                if response.status != 200:
                    return []
                
                html = await response.text()
                soup = bs(html, "html.parser")

                for song in soup.find_all('li', class_="tracks__item"):
                    if len(songs_data) >= limit:
                        break
                    
                    try:
                        data = json.loads(song['data-musmeta'])
                        
                        title = data.get("title", "").strip()
                        artist = data.get("artist", "").strip()
                        
                        download_btn = song.select_one('.track__download-btn[href*=".mp3"]')
                        download_url = download_btn['href'][35:] if download_btn else ""
                        
                        duration_elem = song.select_one('.track__fulltime')
                        duration = duration_elem.get_text(strip=True) if duration_elem else "00:00"
                        if len(download_url) >= 64: # слишком большие ссылки для callback`а
                            continue

                        if artist and title and download_url:
                            song_name = f'{artist} - {title}({duration})'
                            songs_data.append((song_name, download_url))
                                
                    except (KeyError, json.JSONDecodeError, AttributeError):
                        continue
                        
        except Exception as e:
            print(f"Ошибка при запросе к hitmo: {e}")
            return []
    
    return songs_data

#song filter
async def top_songs(music_data, query, top_count=10):
    if not music_data:
        return []
    
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


async def send_downloading_kb(message, url:str, music_data: list = []):
    if not music_data: # если musiс_data пустая
        await message.answer(f'К сожалению, ничего не найдено. <a href="{sites["muzmo"]["base_url"]}">Посмотреть на сайте</a>.', parse_mode="HTML")
        return 

    buttons = []
    if music_data[0] == sites["muzmo"]['code_letter']: #muzmo
        for song, id in music_data[1:]:
            buttons.append(
                [InlineKeyboardButton(
                text=song,
                callback_data=sites["muzmo"]['code_letter']+id
                )]
                )
        kb = InlineKeyboardMarkup(inline_keyboard=buttons)
        await message.answer(
            f'Музыка найдена на сайте <a href="{sites["muzmo"]["base_url"]}">Muzmo</a>.  <a href="{sites["muzmo"]["base_url"]+url}">На сайт</a>.',
            parse_mode="HTML",
            reply_markup=kb
            )

    elif music_data[0] == sites["hitmo"]['code_letter']: #hitmo
        for song, href in music_data[1:]:
            buttons.append(
                [InlineKeyboardButton(
                text=song,
                callback_data=sites["hitmo"]['code_letter']+href
                )]
                )
        kb = InlineKeyboardMarkup(inline_keyboard=buttons)
        await message.answer(
            f'Музыка найдена на сайте <a href="{sites["hitmo"]["base_url"]}">Hitmo</a>.  <a href="{sites["hitmo"]["base_url"]+url}">На сайт</a>.',
            parse_mode="HTML",
            reply_markup=kb
            )

    
# Button handler
@dp.callback_query()
async def download_song(callback: CallbackQuery):
    data = callback.data
    if data[0] == sites["muzmo"]["code_letter"]: # Обработка кнопок от muzmo
        id = data[1:]
        link = sites["muzmo"]["base_url"] + "/info?id=" + id
        song = "не_найдена"
        for row in callback.message.reply_markup.inline_keyboard:
            for button in row:
                if button.callback_data == data:
                    song = button.text
        pattern = r'\s*\(\d+:\d+\)\s*$'
        song_without_timer = re.sub(pattern, "", song).strip()
        filename = song_without_timer.replace(" ", "_").replace("/", "_") + ".mp3" 
        download_url = await get_downloadlink(link)
        if not download_url:
            download_url = await get_downloadlink(link)
            if not download_url:
                await callback.answer("Не удалось получить ссылку на скачивание.", show_alert=True)
                return

    elif data[0] == sites["hitmo"]["code_letter"]: # Обработка кнопок от hitmo
        href = data[1:]
        download_url = sites["hitmo"]["base_download_url"]+href

        song = "не_найдена"
        for row in callback.message.reply_markup.inline_keyboard:
            for button in row:
                if button.callback_data == data:
                    song = button.text
        pattern = r'\s*\(\d+:\d+\)\s*$'
        song_without_timer = re.sub(pattern, "", song).strip()
        filename = song_without_timer.replace(" ", "_").replace("/", "_") + ".mp3" 
        
    await download(callback=callback, filename=filename, download_url=download_url)
    await callback.answer()

# Downloading
async def download(callback, filename: str, download_url: str):
    try:
        async with aiohttp.ClientSession() as session:
            async with semaphore, session.get(download_url) as response:
                file_size = int(response.headers.get("Content-Length", 0))

                if file_size > FILE_SIZE_LIMIT:  # если больше N Мб отменяет скачивание 
                    await callback.answer(f"Файл слишком большой ({file_size / 1024 / 1024:.2f} MB). Лимит: {FILE_SIZE_LIMIT / 1024 / 1024} MB.", show_alert=True)
                    return

                await callback.answer()# проблема долгого ответа
                performer = filename.split("_-_")[0].strip("_").replace("_", " ")
                title = filename.split("_-_")[1].strip("_").split(".mp3")[0].strip("_").replace("_", " ")
                if file_size < 20 * 1024 * 1024: # если меньше 20Мб отправляет напрямую 
                    await bot.send_chat_action(callback.message.chat.id, 'upload_document')
                    audio_file = URLInputFile(
                        url=download_url,
                        filename=filename
                    )
                    await callback.message.answer_audio(audio_file, title=title, performer=performer)
                    return

                await bot.send_chat_action(callback.message.chat.id, "record_voice")  # если больше 20Мб скачивет, а потом отправляет 
                with open(filename, 'wb') as f:
                    async for chunk in response.content.iter_chunked(2048):
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
                            return sites["muzmo"]["base_url"]+href
        except Exception as ex:
            print(f"[!] (get_downloadlink) Ошибка получения ссылки: {ex}")

    return None

async def main():
    
    await dp.start_polling(bot)

# Bot startup
if __name__ == "__main__":
    asyncio.run(main())
