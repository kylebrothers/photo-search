FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 5000

# --timeout 90: must stay LARGER than query_parser_llm.py's Ollama request
# timeout (60s), so a slow LLM call fails gracefully into that module's own
# timeout/fallback handling rather than Gunicorn killing the worker first,
# which was producing raw connection drops ("Network error" in the browser)
# instead of a clean JSON fallback response. Bumped 2026-07-11.
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "--timeout", "90", "app:app"]
