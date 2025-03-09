# https://www.mktr.ai/the-data-scientists-quick-guide-to-dockerfiles-with-examples/

###############################################
# Base Image
###############################################
FROM python:3.9-slim

# Combine all ENV variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=off \
    PIP_DISABLE_PIP_VERSION_CHECK=on \
    PIP_DEFAULT_TIMEOUT=100 \
    POETRY_VERSION=1.4.1 \
    POETRY_HOME="/opt/poetry" \
    POETRY_VIRTUALENVS_IN_PROJECT=true \
    POETRY_NO_INTERACTION=1 \
    PYSETUP_PATH="/opt/pysetup" \
    VENV_PATH="/opt/pysetup/.venv" \
    PATH="$POETRY_HOME/bin:$VENV_PATH/bin:$PATH"

# Install system dependencies and poetry in one layer
RUN apt-get update && \
    apt-get install --no-install-recommends -y \
    build-essential \
    curl && \
    rm -rf /var/lib/apt/lists/* && \
    pip install "poetry==$POETRY_VERSION" uvicorn

WORKDIR $PYSETUP_PATH

# Copy only what's needed for dependency installation
COPY pyproject.toml poetry.lock ./

# Install dependencies including FastAPI and uvicorn
RUN poetry config virtualenvs.create false \
    && poetry install --no-dev \
    && poetry add fastapi uvicorn

# Copy application code
COPY . .

EXPOSE 8000

CMD ["uvicorn", "nextflow_telemetry.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
