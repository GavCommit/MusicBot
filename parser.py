
import aiohttp
import asyncio
import difflib
from bs4 import BeautifulSoup

muzmo_baselink = "https://rmr.muzmo.cc"



#def get_music(query: str):   # синхронный вариант
#    import requests
#    import time   
#    search_query = query.replace(" ", "+")
#
#    music_data = []
#    for start in [0, 15]:
#        url = f"{muzmo_baselink}/search?q={search_query}&start={start}"
#
#        response = requests.get(url)
#        if response.status_code != 200:
#            if music_data:
#                return music_data
#            else:
#                return {"success" : False, "error" : f"Responce code: {response.status_code}"}
#
#        data = bs(response.text, "html.parser")
#        names = data.find_all('a', class_="block")
#        for item in names:
#            href = item.get('href', '')
#
#            #проверка, что это ссылка на песню
#            if href.startswith(('/info?id')):   # '/get_new?',  но не всегда скачивается
#                clear_item = item.text.strip()
#
#                #проверка что это песня
#                if " - " in clear_item and "(" in clear_item:
#                    try:
#                        name_part = clear_item.split('(')[0].strip()
#                        time_part = clear_item.split('(')[1].split(',')[0].strip()
#                        song_name = f"{name_part}({time_part})"
#
#                        music_data.append((song_name, muzmo_baselink + href))
#                    except IndexError:
#                        continue
#    return music_data





async def get_music(query: str, pages: int = 2) -> list:
    base_url = "https://rmr.muzmo.cc"
    
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
                soup = BeautifulSoup(html, "html.parser")
                
                for item in soup.find_all('a', class_="block"):
                    href = item.get('href', '')
                    if href.startswith(( '/info?id')):   # '/get_new?',  но не всегда скачивается
                        text = item.get_text(strip=True)
                        if " - " in text and "(" in text:
                            try:
                                name = text.split('(')[0].strip()
                                time = text.split('(')[1].split(',')[0].strip()
                                all_music_data.append((
                                    f"{name}({time})",
                                    f"{base_url}{href}"
                                ))
                            except IndexError:
                                continue
            
        return all_music_data


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


def output_print(query, music_data, music_data_filtered):
    print("Запрос: " + query)
    print(f'{"Фильтровнанные результаты":_^50}')
    for song in music_data_filtered:
        print(song[0])

    print("\n\n"+ "_"*50)
    print("Всего результатов: "+ str(len(music_data_filtered))+ "\n\n")


    print(f'{"Обычные результаты":_^50}')
    for song in music_data[:len(music_data_filtered)]:
        print(song[0])

# 
async def main():
    query = "Игорь Тальков - я вернусь"

    music_data = await get_music(query=query, pages=4) # делаем запросы к сайту (асинхрон, несколько страниц)

    music_data_filtered = await top_songs(music_data, query) # фильтруем результат поиска, находим наибольшее совпадение

    output_print(query, music_data, music_data_filtered)

if __name__ == "__main__":
    asyncio.run(main())

 