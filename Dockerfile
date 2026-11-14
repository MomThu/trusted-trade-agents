# Valio Aimo – Trusted Trade Agents (one process: MCP + REST + approval UI)
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8000
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# Valio's data files are NOT in the image: deploy/start.sh downloads them with VALIO_API_KEY on first start.
EXPOSE 8000
CMD ["sh", "deploy/start.sh"]
