FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/app/data
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN pip install --no-cache-dir uv==0.11.6 && uv sync --frozen --no-dev
COPY app ./app
RUN useradd --uid 10001 --create-home briefing && mkdir /app/data && chown briefing:briefing /app/data
USER briefing
EXPOSE 8000
CMD ["/app/.venv/bin/uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-access-log"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 CMD /app/.venv/bin/python -c "import os,urllib.request,base64; r=urllib.request.Request('http://127.0.0.1:8000/health'); r.add_header('Authorization','Basic '+base64.b64encode((os.environ['APP_USERNAME']+':'+os.environ['APP_PASSWORD']).encode()).decode()); assert urllib.request.urlopen(r).status==200"
