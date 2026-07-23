FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY db.py rate_limiter.py scraper.py crawler.py entrypoint.py webapp.py ./
COPY web/ web/

CMD ["python3", "entrypoint.py"]
