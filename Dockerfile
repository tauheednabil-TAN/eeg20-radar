# python 3.12.16-slim, pinned by digest
FROM python:3.12-slim@sha256:aa943ce411423c7582288f18707ff66eddcbaeb510502d747a72aeb34f30d159

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

WORKDIR /app

ARG INSTALL_DEV=false
COPY requirements.txt requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
 && if [ "$INSTALL_DEV" = "true" ]; then pip install --no-cache-dir -r requirements-dev.txt; fi

COPY pyproject.toml ./
COPY src ./src
COPY fixtures ./fixtures
COPY tests ./tests
COPY sql ./sql

RUN useradd --create-home --uid 10001 radar && mkdir -p /app/output && chown radar /app/output
USER radar

ENTRYPOINT ["python", "-m", "eeg20_radar"]
CMD ["run"]
