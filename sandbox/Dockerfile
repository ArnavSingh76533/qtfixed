FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends chromium ffmpeg && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir pillow==11.3.0 pypdf==6.1.1 python-docx==1.2.0 openpyxl==3.1.5 requests==2.32.5 beautifulsoup4==4.13.5 playwright==1.55.0 numpy==2.3.3 matplotlib==3.10.6
COPY runner.py /runner.py
COPY web_proxy.py public_web.py sandbox_dependencies.py /
USER 65534:65534
WORKDIR /workspace
ENTRYPOINT []
