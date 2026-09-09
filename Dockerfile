# FXCM daemon Docker 镜像（Linux x86_64）
#
# 状态：结构就绪，待 fxcorepy Linux 绑定补齐（见 docs/DOCKER_SPIKE.md 三条出路）。
# TODO(build): 获取 fxcorepy Linux 二进制后，按注释中的步骤解开即可构建。

FROM python:3.10-slim

# 运行时依赖：libsqlite3 已内置；forexconnect 原生库依赖见 SDK/lib
RUN apt-get update && apt-get install -y --no-install-recommends \
        libstdc++6 liblog4cplus-2.0.5 2>/dev/null; \
    rm -rf /var/lib/apt/lists/* || true

WORKDIR /app

# 1) 纯 Python 包装层（来自 mac wheel 的平台无关部分）
# COPY site-packages/forexconnect/*.py ./forexconnect/
# 2) fxcorepy Linux 绑定（出路 A：FXCM 提供；出路 B：ctypes 绑定产物）
# COPY fxcorepy_linux/fxcorepy*.so ./forexconnect/lib/

# 3) 应用代码
COPY fxcm_api/ ./fxcm_api/
COPY static/ ./static/
COPY requirements.txt .

RUN pip install --no-cache-dir --index-url https://pypi.tuna.tsinghua.edu.cn/simple \
        -r requirements.txt

# 会话缓存与数据库目录
RUN mkdir -p data History
VOLUME ["/app/data"]

EXPOSE 8911
CMD ["python", "-m", "fxcm_api.daemon", "--config", "config.demo.json", \
     "--config-real", "config.json"]
