FROM python:3.10

RUN python -m pip install --upgrade wheel poethepoet
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
VOLUME ["/api_new", "/bridge_new"]

COPY ./ ./

RUN mkdir -p ./temporalio/api
RUN uv sync --no-install-project --all-extras
RUN uv add "protobuf<4"
RUN poe gen-protos

CMD cp -r ./temporalio/api/* /api_new && cp -r ./temporalio/bridge/proto/* /bridge_new
