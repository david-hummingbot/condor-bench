# Mock hummingbot-api for the condor-bench mock lane. No exchange, no keys.
FROM python:3.12-slim
RUN pip install --no-cache-dir "fastapi>=0.110" "uvicorn>=0.29"
WORKDIR /app
COPY __init__.py server.py ./mock_mcp/
COPY fixtures ./mock_mcp/fixtures
EXPOSE 18000
CMD ["uvicorn", "--factory", "mock_mcp.server:create_app", "--host", "0.0.0.0", "--port", "18000"]
