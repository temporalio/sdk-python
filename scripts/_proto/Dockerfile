FROM python:3.10

RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="$PATH:/root/.local/bin"
RUN uv tool install poethepoet
VOLUME ["/api_new", "/bridge_new"]

COPY ./ ./

RUN mkdir -p ./temporalio/api
# The legacy compiler needs Python 3.10, below the SDK's minimum version.
RUN uv venv --python /usr/local/bin/python /opt/proto
RUN printf 'setuptools<82\n' > /tmp/build-constraints.txt && \
    uv pip install --python /opt/proto/bin/python \
    --build-constraint /tmp/build-constraints.txt \
    "googleapis-common-protos==1.70.0" "grpcio-tools==1.48.2" \
    "mypy-protobuf>=3.3.0,<4" "protobuf>=3.20,<4" "setuptools<82"
ENV PATH="/opt/proto/bin:$PATH"
RUN /opt/proto/bin/python scripts/gen_protos.py

CMD ["sh", "-c", "cp -r ./temporalio/api/* /api_new && cp -r ./temporalio/bridge/proto/* /bridge_new"]
