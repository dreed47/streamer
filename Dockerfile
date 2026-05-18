FROM python:3.12-slim

RUN apt-get update && apt-get install -y ffmpeg curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN playwright install chromium --with-deps

COPY monitor.py .

VOLUME /recordings
CMD ["python", "monitor.py"]
