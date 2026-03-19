FROM python:3.11-slim

# System dependencies for numpy/scipy (BLAS/LAPACK) and compiled extensions
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        gfortran \
        libopenblas-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY . .

# Install the package with MCP + HTTP transport dependencies (uvicorn, PyJWT, httpx)
RUN pip install --no-cache-dir ".[http]"

# Create non-root user for runtime
RUN useradd -r -s /usr/sbin/nologin oas && \
    mkdir -p /data && chown oas:oas /data

# Artifact storage root inside the container (bind-mounted from host ./oas_data)
ENV OAS_DATA_DIR=/data

# Default transport — override with OAS_TRANSPORT=http for streamable HTTP
ENV OAS_TRANSPORT=stdio

VOLUME /data

EXPOSE 8000

USER oas

ENTRYPOINT ["oas-mcp"]
