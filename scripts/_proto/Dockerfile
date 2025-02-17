FROM python:3.10

RUN curl -LsSf https://astral.sh/uv/install.sh | sh
RUN uv tool install poethepoet
VOLUME ["/api_new", "/bridge_new"]

COPY ./ ./

RUN mkdir -p ./temporalio/api
RUN uv sync --all-extras
RUN uv add "protobuf<4"
RUN poe gen-protos

CMD cp -r ./temporalio/api/* /api_new && cp -r ./temporalio/bridge/proto/* /bridge_new
