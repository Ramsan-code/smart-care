FROM python:3.12-slim-bookworm AS app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends gcc pkg-config default-libmysqlclient-dev mariadb-client openssl && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt requirements-staging.txt ./
RUN pip install --no-cache-dir -r requirements-staging.txt && pip check
COPY . .
RUN DJANGO_SECRET_KEY=build-only-placeholder-not-an-injected-runtime-secret DB_PASSWORD=build-only DJANGO_DEBUG=true DEMO_MODE=true python manage.py collectstatic --noinput     && useradd --uid 10001 --create-home smartcare     && mkdir -p /run/smartcare /var/lib/smartcare && chown -R 10001:10001 /run/smartcare /var/lib/smartcare /app/.runtime
USER 10001:10001
CMD ["gunicorn", "-c", "deploy/gunicorn.conf.py", "smartcare.wsgi:application"]

FROM nginx:1.28-alpine AS proxy
COPY deploy/nginx.conf /etc/nginx/nginx.conf
COPY --from=app /app/.runtime/static /srv/static
USER 10001:10001
ENTRYPOINT ["nginx"]
CMD ["-g", "daemon off;"]
