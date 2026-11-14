FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    COLLECTOR_DB=/data/collector.db \
    COLLECTOR_FILES=/files \
    MAIL_ROOT=/srv/mail

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY app /app/app
COPY imap_server /app/imap_server

RUN mkdir -p /data /files /srv/mail

# API 服务端口 8000，本地 IMAP 样例服务端口 10143
EXPOSE 8000 10143

COPY scripts/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
CMD ["api"]
