# Runs the programs in a container. The database stays on Neon.
# From the backend folder:
#
#   docker build -t eudr-storage .
#   docker run --rm -v "$(pwd):/project" eudr-storage database/test_connection.py
#   docker run --rm -v "$(pwd):/project" -v "D:/eudr/inputs:/inputs" eudr-storage pipeline.py \
#       --name jambi --data-dir /inputs --bbox 103.0 -2.3 103.5 -1.8 --test-plots 1000 --verify
#   docker run --rm -v "$(pwd):/project" eudr-storage compare/compare.py layers/jambi --map jambi
#
# "-v ...:/project" shares your backend folder with the container, so it reads your .env and writes
# layers/ and the results/ folders back to your computer; the second -v shares your satellite layers.
# (Windows PowerShell: use "${PWD}:/project" instead of "$(pwd):/project".)
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

WORKDIR /project
COPY . /project

ENTRYPOINT ["python"]
CMD ["database/test_connection.py"]
