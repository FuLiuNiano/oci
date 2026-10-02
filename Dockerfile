FROM python:3.12-slim

WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends netcat-openbsd ca-certificates && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

ENV PORT=9527 PYTHONUNBUFFERED=1
EXPOSE 9527
VOLUME ["/app/data"]

CMD ["python", "main.py"]
