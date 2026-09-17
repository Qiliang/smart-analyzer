FROM astral/uv:python3.11-trixie
WORKDIR /app
ENV TZ=Asia/Shanghai
COPY . .
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && uv sync --frozen --no-cache --no-install-project --no-dev

# 启动fastapi: uv run fastapi run
CMD [ "uv", "run", "fastapi","run","--port","8080"]
