FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Форсируем пересборку – эта строка всегда новая, чтобы Render не использовал кеш
RUN echo "Build timestamp: $(date)"

COPY . .

CMD ["python", "-m", "main"]
