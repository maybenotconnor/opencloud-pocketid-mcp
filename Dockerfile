# =============================================================================
# OpenCloud MCP Server
# Multi-stage build using uv for fast, reproducible dependency resolution
# =============================================================================

# --- Stage 1: Build dependencies ---
FROM ghcr.io/astral-sh/uv:python3.11-bookworm-slim AS builder

WORKDIR /app

# Enable bytecode compilation for faster startup
ENV UV_COMPILE_BYTECODE=1
# Use copy mode so the venv is self-contained (no symlinks to cache)
ENV UV_LINK_MODE=copy

# Install dependencies first (layer caching: only re-runs if lock changes)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Copy source and install the project itself
COPY src/ ./src/
RUN uv sync --frozen --no-dev

# --- Stage 2: Runtime ---
FROM python:3.11-slim-bookworm AS runtime

WORKDIR /app

# Create non-root user
RUN groupadd --gid 1000 mcp && \
    useradd --uid 1000 --gid mcp --shell /bin/bash --create-home mcp

# Copy the complete venv from builder
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src ./src

# Put the venv on PATH
ENV PATH="/app/.venv/bin:$PATH"

# Switch to non-root user
USER mcp

# Expose port (configurable via MCP_PORT, default 8000)
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

# Run with uvicorn
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
