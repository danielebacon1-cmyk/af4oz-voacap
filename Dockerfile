FROM debian:trixie-slim
RUN apt-get update && apt-get install -y --no-install-recommends python3 voacapl && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY app.py .
USER 65534:65534
CMD ["python3", "app.py"]
