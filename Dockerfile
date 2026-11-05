FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY davomat_bot.py .
ENV PYTHONUNBUFFERED=1
CMD ["python", "davomat_bot.py"]
