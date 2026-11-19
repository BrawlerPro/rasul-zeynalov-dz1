FROM python:3.12.15-slim
WORKDIR /service
COPY load/requirements.lock.txt /tmp/requirements.lock.txt
RUN pip install --no-cache-dir -r /tmp/requirements.lock.txt
COPY . .
