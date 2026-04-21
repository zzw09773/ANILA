FROM python:3.11-slim

WORKDIR /app

# Install anila-core from the monorepo (adjust path or use PyPI release)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 9100

CMD ["uvicorn", "agent:app", "--host", "0.0.0.0", "--port", "9100"]
