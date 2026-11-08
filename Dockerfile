FROM node:22-bookworm-slim AS ui
WORKDIR /build
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim
WORKDIR /app
COPY backend/requirements.lock.txt ./requirements.lock.txt
RUN pip install --no-cache-dir -r requirements.lock.txt
COPY backend/ ./backend/
COPY --from=ui /build/dist ./frontend/dist
ENV DATA_DIR=/app/data SOURCE_ROOT=/materials PYTHONUNBUFFERED=1
WORKDIR /app/backend
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
