import os, re, json
import aiohttp
import asyncio
import yt_dlp
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
FILE_SIZE_LIMIT = config.getint("Settings", "FILE_SIZE_LIMIT") * 1024 * 1024 # in bites
PAGES_SCANNING = config.getint("Settings", "PAGES_SCANNING")
SEARCH_RESULTS = config.getint("Settings", "SEARCH_RESULTS")
SITE_PRIORITY = config.get("Settings", "SITE_PRIORITY", fallback="muzmo").split(",")
MIN_RESULTS = config.getint("Settings", "MIN_RESULTS")
PROXY_URL = config.get("Settings", "PROXY_URL", fallback=None)

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

sites = {
    "hitmo": {
        "code_letter": "b",
        "base_url": "https://rus.hitmoz.org",
        "base_download_url": "https://rus.hitmoz.org/get/music/"
    },
    "muzmo":{
    "code_letter": "a",
        "base_url": "https://rmr.muzmo.cc",
        "base_download_url": False
    }
}

bot = 0

if PROXY_URL:
    from aiogram.client.session.aiohttp import AiohttpSession
    session = AiohttpSession(proxy=PROXY_URL)
    bot = Bot(token=TOKEN, session=session)
    logger.info(f"Starting with proxy: {PROXY_URL}")
else:
    bot = Bot(token=TOKEN)
dp = Dispatcher()

semaphore = asyncio.Semaphore(10)

# /start
@dp.message(F.chat.type == "private", Command(commands=['start','старт','отвинта']))
async def start(message: Message):
    greeting = f"Здравствуйте, {message.from_user.first_name}! Этот бот поможет вам найти и скачать музыку."
    await message.answer(greeting)
    await message.answer("Введите название песни или исполнителя:")

#check for youtube url
async def is_youtube_url(url: str) -> bool:
    patterns = [
        r'(?:https?:\/\/)?(?:www\.)?youtube\.com\/watch\?v=[\w-]+',
        r'(?:https?:\/\/)?(?:www\.)?youtu\.be\/[\w-]+',
        r'(?:https?:\/\/)?(?:www\.)?youtube\.com\/shorts\/[\w-]+',
        r'(?:https?:\/\/)?(?:www\.)?youtube\.com\/embed\/[\w-]+',
    ]
    for pattern in patterns:
        if re.match(pattern, url):
            return True
    return False

# Message handler (music search)
@dp.message(F.text)
async def handle_text(message: Message):
    query = message.text.strip()
    if len(query) < 3:
        await message.answer("Запрос для поиска не менее 3х символов")
        return
    if query[:8] == "https://":
        if await is_youtube_url(query):

            filename, metadata = await download_from_yt(message=message, url=query)
            if metadata:
                performer = metadata.get("artist", "Unknown")
                title = metadata.get("title", "Unknown")
            else:
                performer = "Unknown"
                title = "Unknown"

            class CallbackMock:
                def __init__(self, message):
                    self.message = message
            callback = CallbackMock(message=message)
            if filename:
                await send_file(callback=callback, filename=filename, title=title, performer=performer)
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
    best_data = []
    best_site =""
    best_count = 0

    for site_name in SITE_PRIORITY:
        site_name = site_name.strip()
        if site_name == "muzmo":
            music_data = await search_music_muzmo(query=query, pages=PAGES_SCANNING) # делаем запросы к сайту muzmo (асинхрон, несколько страниц)
        elif site_name == "hitmo":
            music_data = await search_music_hitmo(query=query) # делаем запросы к сайту hitmo
        else:
            logger.error("Site for serching not in available sites list(muzmo, hitmo)")
            continue # не найден

        if music_data:
            current_count = len(music_data)
            if current_count > best_count:
                best_data = music_data
                best_site =  sites.get(site_name)["code_letter"]
                best_count = current_count

                if current_count >= MIN_RESULTS:
                    break

    if best_data:
        return sites.get(site_name)["code_letter"], music_data
    else:
        return '', []

# Async music parser muzmo
async def search_music_muzmo(query: str, pages: int = 3) -> list:
    query = query.strip().replace(" ", "+")

    connector = aiohttp.TCPConnector(limit_per_host=pages)
    async with aiohttp.ClientSession(connector=connector) as session:
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
                soup = await asyncio.to_thread(bs, html, "html.parser")

                for item in soup.find_all('a', class_="block"):
                    href = item.get('href', '')
                    if href.startswith(('/get_new?','/info?id')):   #   но не всегда скачивается  НЕ ДОБАВЛЯТЬ СЛОМАЕТ CALLBACK
                        text = item.get_text(strip=True)
                        if " - " in text and "(" in text:
                            try:
                                name = text.split('(')[0].strip()
                                time = text.split('(')[1].split(',')[0].strip()
                                all_music_data.append((
                                    f"{name}({time})",
                                    #f"{href[9:]}" # {base_url} получаем просто id песни
                                    f"{'A::'+href[9:] if href.startswith('/info?id') else 'B::'+href[13:]}"
                                ))
                            except IndexError:
                                continue

        return all_music_data


#Async music parser hitmo
async def search_music_hitmo(query: str, limit: int = 40) -> list:
    query = query.strip().replace(" ", '-')
    url = f"{sites['hitmo']['base_url']}/search?q={query}"

    songs_data = []

    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as response:
                if response.status != 200:
                    return []

                html = await response.text()
                soup = await asyncio.to_thread(bs, html, "html.parser")

                for song in soup.find_all('li', class_="tracks__item"):
                    if len(songs_data) >= limit:
                        break

                    try:
                        data = json.loads(song['data-musmeta'])

                        title = data.get("title", "").strip()
                        artist = data.get("artist", "").strip()

                        download_btn = song.select_one('.track__download-btn[href*=".mp3"]')
                        download_url = download_btn['href'][11:] if download_btn else ""

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
            logger.info(f"Ошибка при запросе к hitmo: {e}")
            return []

    return songs_data

#async difflib sort
def _sync_top_songs_calc(music_data, query_lower):
    chunk_scores = []
    for song in music_data:
        song_lower = song[0].lower()
        similarity = difflib.SequenceMatcher(None, query_lower, song_lower).ratio()
        chunk_scores.append((similarity, song))
    
    # Сортируем по убыванию рейтинга схожести
    chunk_scores.sort(key=lambda x: x[0], reverse=True)
    return [song for score, song in chunk_scores]

#song filter
async def top_songs(music_data, query: str, top_count=10):
    if not music_data:
        return []

    if len(music_data) <= top_count:
        return music_data

    query_lower = query.lower()
    sorted_songs = await asyncio.to_thread(_sync_top_songs_calc, music_data, query_lower)

    return sorted_songs[:top_count]

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
    await callback.answer()
    
    data = callback.data
    download_url = None
    filename = None
    
    if data[0] == sites["muzmo"]["code_letter"]: # Обработка кнопок от muzmo
        id = data[1:]
        if id[:3] == "A::": # разные виды кнопок
            link = sites["muzmo"]["base_url"] + "/info?id=" + id[3:] 
        else:
            link = sites['muzmo']['base_url'] + "/get_new?get=" + id[3:]
        
        filename = await get_filename_from_button(callback.message.reply_markup.inline_keyboard, data)

        download_url = await get_downloadlink(link)
        if not download_url:
            download_url = await get_downloadlink(link)
            if not download_url:
                await callback.answer("Не удалось получить ссылку на скачивание.", show_alert=True)
                return

    elif data[0] == sites["hitmo"]["code_letter"]: # Обработка кнопок от hitmo
        href = data[1:]
        download_url = sites["hitmo"]["base_download_url"]+href

        filename = await get_filename_from_button(callback.message.reply_markup.inline_keyboard, data)
    else:
        logger.error("ID of button isn`t recognized")
        pass

    await download(callback=callback, filename=filename, url=download_url)

async def get_filename_from_button(button_mas: list, data: list) -> str:
    song = None
    for row in button_mas:
        for button in row:
            if button.callback_data == data:
                song = button.text
    pattern = r'\s*\(\d+:\d+\)\s*$'
    song_without_timer = re.sub(pattern, "", song if song else "не_найдена").strip()
    filename = song_without_timer.replace(" ", "_").replace("/", "_") + ".mp3"
    return filename

#sync function for file saving
def _save_chunk_to_file(filepath, chunk, mode='ab'):
    with open(filepath, mode) as f:
        f.write(chunk)

# Downloading
async def download(callback, filename: str, url: str):
    try:
        parts = filename.split("_-_")
        performer = parts[0].strip("_").replace("_", " ") if len(parts) > 1 else "Unknown"
        title = parts[1].strip("_").split(".mp3")[0].strip("_").replace("_", " ") if len(parts) > 1 else "Track"

        if os.path.exists(filename):
            os.remove(filename)

        await bot.send_chat_action(callback.message.chat.id, 'record_voice')

        async with aiohttp.ClientSession() as session:
            async with semaphore, session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as response:
                if response.status != 200:
                    await callback.message.answer("Сайт с музыкой не отдал файл. Попробуйте другую ссылку.")
                    return

                file_size = int(response.headers.get("Content-Length", 0))
                if file_size > FILE_SIZE_LIMIT:
                    await callback.message.answer(
                        f"Файл слишком большой ({file_size / 1024 / 1024:.2f} MB). Лимит: {FILE_SIZE_LIMIT / 1024 / 1024} MB."
                    )
                    return

                first_chunk = True
                async_chunks = response.content.iter_chunked(64 * 1024)
                
                async for chunk in async_chunks:
                    mode = 'wb' if first_chunk else 'ab'
                    await asyncio.to_thread(_save_chunk_to_file, filename, chunk, mode)
                    first_chunk = False        
            
        await send_file(callback=callback, filename=filename, title=title, performer=performer)
    except asyncio.TimeoutError:
        logger.warning(f"[!] (download) Таймаут скачивания трека: {url}")
        await callback.message.answer("Превышено время ожидания скачивания трека.")
    except Exception as ex:
        logger.info(f"[!] (download) Ошибка загрузки: {ex}")
        await callback.answer("Ошибка при обработке. Попробуйте снова.", show_alert=True)

#download from YT using yt-dlp 
async def download_from_yt(message, url: str) -> tuple[str, dict]:
    try:
        await message.answer("Скачивание музыки с ютуба, подождите.")
        await bot.send_chat_action(message.chat.id, 'record_video')
        
        # get video info
        opts_info = {
            'quiet': True,
            'extractor_args': {'youtube': {'player_client': ['tv_embedded']}}
        }

        with yt_dlp.YoutubeDL(opts_info) as ydl:
            info = await asyncio.to_thread(ydl.extract_info, url, download=False)

        # searching best format
        best_audio = None
        for f in info['formats']:
            if f.get('acodec') != 'none' and f.get('vcodec') == 'none':
                if best_audio is None or f.get('abr', 0) > best_audio.get('abr', 0):
                    best_audio = f

        if not best_audio:
            logger.info(f"[!] (download_from_yt) Формат аудио не найден. Ошибка загрузки: {ex}")
            return None, None

        # check max file size
        size_bytes = best_audio.get('filesize') or best_audio.get('filesize_approx', 0)

        if size_bytes > FILE_SIZE_LIMIT:
            await message.answer(
                f"Файл слишком большой ({size_bytes / 1024 / 1024:.2f} MB). Лимит: {FILE_SIZE_LIMIT / 1024 / 1024} MB."
            )
            return None, None    

        # downloading
        safe_name = info['title'].replace(" ", "_").replace("/", "_")
        opts_download = {
            'format': best_audio['format_id'],
            'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3'}],
            'outtmpl': f"{safe_name}.%(ext)s",
            'quiet': True,
            'extractor_args': {'youtube': {'player_client': ['tv_embedded']}}
        }

        with yt_dlp.YoutubeDL(opts_download) as ydl:
            await asyncio.to_thread(ydl.extract_info, url, download=True)

        # return file
        filename = f"{safe_name}.mp3"
        metadata = {
            'title': info['title'],
            'artist': info['uploader']
        }

        return filename, metadata

    except Exception as ex:
        logger.info(f"[!] (download_from_yt) Ошибка загрузки: {ex}")
        await message.answer("Ошибка при загрузке видео. Попробуйте снова.", show_alert=True)
        return None, None

# sending song file from local 
async def send_file(callback, filename: str, title: str, performer: str):
    try:
        audio_file = FSInputFile(filename, filename=filename)
        await bot.send_chat_action(callback.message.chat.id, 'upload_document')
     
        await callback.message.answer_audio(
            audio=audio_file,
            title=title,
            performer=performer,
            timeout=90 
        )  
        
    except Exception as ex:
        logger.error(f"[!] (send_file) Ошибка отправки: {ex}")
        await callback.message.answer("Ошибка при отправке файла. Попробуйте другую песню.")
    finally:
        if os.path.exists(filename):
            await asyncio.to_thread(os.remove, filename)

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
            logger.info(f"[!] (get_downloadlink) Ошибка получения ссылки: {ex}")

    return None


async def main():

    await dp.start_polling(bot)

# Bot startup
if __name__ == "__main__":
    asyncio.run(main())