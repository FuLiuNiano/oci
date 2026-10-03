FROM python:3.12-slim

WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends netcat-openbsd ca-certificates && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

ENV HOST=0.0.0.0 PORT=9528 PYTHONUNBUFFERED=1
EXPOSE 9528
VOLUME ["/app/data"]

CMD ["python", "main.py"]
