FROM python:3.11-slim 
RUN apt-get update && apt-get install -y poppler-utils && rm -rf /var/lib/apt/lists/* 
WORKDIR /app 
COPY requirements.txt . 
RUN pip install --no-cache-dir -r requirements.txt 
COPY . . 
RUN mkdir -p uploads outputs 
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "10000"] 
