FROM python:3.10

RUN python -m pip install --upgrade wheel "poetry==1.3.2" poethepoet
VOLUME ["/api_new", "/bridge_new"]

COPY ./ ./

RUN mkdir -p ./temporalio/api
RUN poetry install --no-root -E opentelemetry
RUN poetry add "protobuf<4"
RUN poe gen-protos

CMD cp -r ./temporalio/api/* /api_new && cp -r ./temporalio/bridge/proto/* /bridge_new
