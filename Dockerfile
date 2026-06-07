FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir \
    flask \
    pandas \
    numpy \
    scipy \
    pyproj \
    shapely \
    requests \
    pyarrow

COPY app.py .
COPY pull_*.py .

# Copy default data if available (volume mount overrides at runtime)
COPY output/incidents_24mo.parquet /data/

ENV DATA_PATH=/data/incidents_24mo.parquet
ENV CONFIG_PATH=/data/config.json

EXPOSE 5000

CMD ["python", "app.py"]
