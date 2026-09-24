FROM python:3.12-slim
WORKDIR /app
COPY app.py lb.py ./
RUN pip install --no-cache-dir fastapi uvicorn httpx
EXPOSE 8000
ENV BACKENDS=""
CMD ["python", "app.py", "--host", "0.0.0.0", "--port", "8000"]
