FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY nova_server.py config.json config.local.example.json ./
COPY static/ static/
COPY systemd/ systemd/

EXPOSE 8080

CMD ["uvicorn", "nova_server:app", "--host", "0.0.0.0", "--port", "8080"]
