FROM python:3.12-slim
WORKDIR /app
COPY main.py requirements.txt ./
ENV WS_PATH=/tk-rail
EXPOSE 8080
CMD ["python3", "main.py"]
