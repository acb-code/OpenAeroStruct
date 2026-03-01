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

# Artifact storage location inside the container
ENV OAS_DATA_DIR=/data/artifacts

# Default transport — override with OAS_TRANSPORT=http for streamable HTTP
ENV OAS_TRANSPORT=stdio

VOLUME /data

EXPOSE 8000

ENTRYPOINT ["oas-mcp"]
