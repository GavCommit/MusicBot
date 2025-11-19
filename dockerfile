FROM python:3.9-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    gcc python3-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
COPY MusicBot_aiogram.py .


RUN pip install --no-cache-dir -r requirements.txt

CMD ["python", "MusicBot_aiogram.py"]
