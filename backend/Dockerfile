# Runs the programs in a container. The database stays on Neon.
# From the project folder (eudr_storage):
#
#   docker build -t eudr-storage .
#   docker run --rm -v "$(pwd):/project" eudr-storage database/test_connection.py
#   docker run --rm -v "$(pwd):/project" eudr-storage database/polygon_approach.py build my_map.tif
#   docker run --rm -v "$(pwd):/project" eudr-storage compare/compare.py my_map.tif my_plots.geojson
#
# "-v ...:/project" shares your project folder with the container, so it reads your .env,
# data/tiff and data/geojson, and writes the results/ folders back to your computer.
# (Windows PowerShell: use "${PWD}:/project" instead of "$(pwd):/project".)
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

WORKDIR /project
COPY . /project

ENTRYPOINT ["python"]
CMD ["database/test_connection.py"]
