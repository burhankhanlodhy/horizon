FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Every API module must be listed here, or the API fails to start.
COPY main.py db.py security.py schema.sql analytics.py billing.py contact.py \
    mailer.py account_email.py turnstile.py stripe_setup.py savings_ledger.py teams.py team_billing.py team_sso.py ./

EXPOSE 8788

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8788"]
