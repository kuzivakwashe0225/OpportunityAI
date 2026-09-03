FROM python:3.12-slim

WORKDIR /app

# Dependencies first so this layer caches independently of app code changes.
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .

COPY . .

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "opportunity_agent.api:app", "--host", "0.0.0.0", "--port", "8000"]
