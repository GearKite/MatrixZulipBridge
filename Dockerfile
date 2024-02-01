FROM docker.io/python:3.12.1-alpine3.19

WORKDIR /opt/matrixzulipbridge
COPY . .

RUN pip install -e . && \
    python -m matrixzulipbridge  -h

EXPOSE 28464/tcp
ENTRYPOINT ["python", "-m", "matrixzulipbridge", "-l", "0.0.0.0"]
CMD []
