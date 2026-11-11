FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN apt-get update && apt-get install -y --no-install-recommends libatomic1 && rm -rf /var/lib/apt/lists/*
RUN playwright install --with-deps chromium
COPY *.py *.html *.md collector.js ./
COPY whatsapp-extension/ ./whatsapp-extension/
ENV HOST=0.0.0.0 PORT=8000 SCOUT_DATA_DIR=/data PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["python", "app.py", "--no-browser"]
