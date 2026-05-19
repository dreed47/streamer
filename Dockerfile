FROM python:3.12-slim

RUN apt-get update && apt-get install -y ffmpeg curl cifs-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN playwright install chromium --with-deps

COPY entrypoint.sh /entrypoint.sh

COPY monitor.py .

VOLUME /recordings
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
