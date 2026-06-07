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
COPY output/incidents_24mo.parquet output/

ENV DATA_PATH=output/incidents_24mo.parquet

EXPOSE 5000

CMD ["python", "app.py"]
