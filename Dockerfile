FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml README.md ./
COPY instadeals ./instadeals
RUN pip install --no-cache-dir . \
    && useradd --uid 10001 --create-home instadeals \
    && mkdir /data && chown instadeals:instadeals /data
USER instadeals
CMD ["instadeals"]
