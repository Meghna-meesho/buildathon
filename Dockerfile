# Runs Insight Lens (FastAPI + build-free React) as a single container.
# Works on Hugging Face Spaces (Docker SDK, app_port 7860) and any Docker host.
FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# main.py resolves the frontend dir from its own path, so cwd just needs to be backend/
WORKDIR /app/backend
EXPOSE 7860
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "7860"]
