# -*- coding: utf-8 -*-
# Generated by the protocol buffer compiler.  DO NOT EDIT!
# source: worker/workflow_sandbox/testmodules/proto/proto_message.proto
"""Generated protocol buffer code."""

from google.protobuf import descriptor as _descriptor
from google.protobuf import descriptor_pool as _descriptor_pool
from google.protobuf import message as _message
from google.protobuf import reflection as _reflection
from google.protobuf import symbol_database as _symbol_database

# @@protoc_insertion_point(imports)

_sym_db = _symbol_database.Default()


from google.protobuf import duration_pb2 as google_dot_protobuf_dot_duration__pb2

DESCRIPTOR = _descriptor_pool.Default().AddSerializedFile(
    b'\n=worker/workflow_sandbox/testmodules/proto/proto_message.proto\x12)worker.workflow_sandbox.testmodules.proto\x1a\x1egoogle/protobuf/duration.proto"?\n\x0bSomeMessage\x12\x30\n\rsome_duration\x18\x01 \x01(\x0b\x32\x19.google.protobuf.Durationb\x06proto3'
)


_SOMEMESSAGE = DESCRIPTOR.message_types_by_name["SomeMessage"]
SomeMessage = _reflection.GeneratedProtocolMessageType(
    "SomeMessage",
    (_message.Message,),
    {
        "DESCRIPTOR": _SOMEMESSAGE,
        "__module__": "worker.workflow_sandbox.testmodules.proto.proto_message_pb2",
        # @@protoc_insertion_point(class_scope:worker.workflow_sandbox.testmodules.proto.SomeMessage)
    },
)
_sym_db.RegisterMessage(SomeMessage)

if _descriptor._USE_C_DESCRIPTORS == False:
    DESCRIPTOR._options = None
    _SOMEMESSAGE._serialized_start = 140
    _SOMEMESSAGE._serialized_end = 203
# @@protoc_insertion_point(module_scope)