FROM python:3.12-slim
RUN pip install --no-cache-dir pillow==11.3.0 pypdf==6.1.1 python-docx==1.2.0 openpyxl==3.1.5
COPY runner.py /runner.py
USER 65534:65534
WORKDIR /workspace
ENTRYPOINT []
