FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml requirements.lock ./
COPY question_bank ./question_bank
RUN pip install --no-cache-dir --require-hashes -r requirements.lock && pip install --no-cache-dir --no-deps . && useradd --create-home app && mkdir /data && chown app:app /data
USER app
EXPOSE 8890
CMD ["question-bank", "serve", "--root", "/data", "--host", "0.0.0.0", "--port", "8890"]
