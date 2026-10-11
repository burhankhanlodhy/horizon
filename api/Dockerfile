FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py db.py security.py schema.sql analytics.py billing.py contact.py stripe_setup.py ./

EXPOSE 8788

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8788"]
