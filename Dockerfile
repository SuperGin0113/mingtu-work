# 使用官方 Python 镜像
FROM luckybirds-registry.cn-shenzhen.cr.aliyuncs.com/luckybirds/python-uv:3.13

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_ENDPOINT=https://hf-mirror.com \
    HF_HUB_DISABLE_SYMLINKS_WARNING=1

WORKDIR /app

COPY requirements.txt .

RUN pip install -i https://mirrors.aliyun.com/pypi/simple -r requirements.txt

COPY . .

EXPOSE 8080


CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "4"]
