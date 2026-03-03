FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# If you need optional cloud backends, uncomment:
# COPY requirements-optional.txt /app/
# RUN pip install --no-cache-dir -r requirements-optional.txt

COPY . /app/

# Render/Heroku-style platforms provide PORT; default is 10000 in code.
CMD ["python", "bot.py"]
