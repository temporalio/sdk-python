# -*- coding: utf-8 -*-
# Generated by the protocol buffer compiler.  DO NOT EDIT!
# source: temporal/api/schedule/v1/message.proto
"""Generated protocol buffer code."""
from google.protobuf import descriptor as _descriptor
from google.protobuf import descriptor_pool as _descriptor_pool
from google.protobuf import message as _message
from google.protobuf import reflection as _reflection
from google.protobuf import symbol_database as _symbol_database

# @@protoc_insertion_point(imports)

_sym_db = _symbol_database.Default()


from google.protobuf import duration_pb2 as google_dot_protobuf_dot_duration__pb2
from google.protobuf import timestamp_pb2 as google_dot_protobuf_dot_timestamp__pb2

from temporalio.api.common.v1 import (
    message_pb2 as temporal_dot_api_dot_common_dot_v1_dot_message__pb2,
)
from temporalio.api.enums.v1 import (
    schedule_pb2 as temporal_dot_api_dot_enums_dot_v1_dot_schedule__pb2,
)
from temporalio.api.workflow.v1 import (
    message_pb2 as temporal_dot_api_dot_workflow_dot_v1_dot_message__pb2,
)

DESCRIPTOR = _descriptor_pool.Default().AddSerializedFile(
    b'\n&temporal/api/schedule/v1/message.proto\x12\x18temporal.api.schedule.v1\x1a\x1egoogle/protobuf/duration.proto\x1a\x1fgoogle/protobuf/timestamp.proto\x1a$temporal/api/common/v1/message.proto\x1a$temporal/api/enums/v1/schedule.proto\x1a&temporal/api/workflow/v1/message.proto"\x95\x01\n\x0c\x43\x61lendarSpec\x12\x0e\n\x06second\x18\x01 \x01(\t\x12\x0e\n\x06minute\x18\x02 \x01(\t\x12\x0c\n\x04hour\x18\x03 \x01(\t\x12\x14\n\x0c\x64\x61y_of_month\x18\x04 \x01(\t\x12\r\n\x05month\x18\x05 \x01(\t\x12\x0c\n\x04year\x18\x06 \x01(\t\x12\x13\n\x0b\x64\x61y_of_week\x18\x07 \x01(\t\x12\x0f\n\x07\x63omment\x18\x08 \x01(\t"1\n\x05Range\x12\r\n\x05start\x18\x01 \x01(\x05\x12\x0b\n\x03\x65nd\x18\x02 \x01(\x05\x12\x0c\n\x04step\x18\x03 \x01(\x05"\x86\x03\n\x16StructuredCalendarSpec\x12/\n\x06second\x18\x01 \x03(\x0b\x32\x1f.temporal.api.schedule.v1.Range\x12/\n\x06minute\x18\x02 \x03(\x0b\x32\x1f.temporal.api.schedule.v1.Range\x12-\n\x04hour\x18\x03 \x03(\x0b\x32\x1f.temporal.api.schedule.v1.Range\x12\x35\n\x0c\x64\x61y_of_month\x18\x04 \x03(\x0b\x32\x1f.temporal.api.schedule.v1.Range\x12.\n\x05month\x18\x05 \x03(\x0b\x32\x1f.temporal.api.schedule.v1.Range\x12-\n\x04year\x18\x06 \x03(\x0b\x32\x1f.temporal.api.schedule.v1.Range\x12\x34\n\x0b\x64\x61y_of_week\x18\x07 \x03(\x0b\x32\x1f.temporal.api.schedule.v1.Range\x12\x0f\n\x07\x63omment\x18\x08 \x01(\t"e\n\x0cIntervalSpec\x12+\n\x08interval\x18\x01 \x01(\x0b\x32\x19.google.protobuf.Duration\x12(\n\x05phase\x18\x02 \x01(\x0b\x32\x19.google.protobuf.Duration"\xba\x04\n\x0cScheduleSpec\x12M\n\x13structured_calendar\x18\x07 \x03(\x0b\x32\x30.temporal.api.schedule.v1.StructuredCalendarSpec\x12\x13\n\x0b\x63ron_string\x18\x08 \x03(\t\x12\x38\n\x08\x63\x61lendar\x18\x01 \x03(\x0b\x32&.temporal.api.schedule.v1.CalendarSpec\x12\x38\n\x08interval\x18\x02 \x03(\x0b\x32&.temporal.api.schedule.v1.IntervalSpec\x12\x44\n\x10\x65xclude_calendar\x18\x03 \x03(\x0b\x32&.temporal.api.schedule.v1.CalendarSpecB\x02\x18\x01\x12U\n\x1b\x65xclude_structured_calendar\x18\t \x03(\x0b\x32\x30.temporal.api.schedule.v1.StructuredCalendarSpec\x12.\n\nstart_time\x18\x04 \x01(\x0b\x32\x1a.google.protobuf.Timestamp\x12,\n\x08\x65nd_time\x18\x05 \x01(\x0b\x32\x1a.google.protobuf.Timestamp\x12)\n\x06jitter\x18\x06 \x01(\x0b\x32\x19.google.protobuf.Duration\x12\x15\n\rtimezone_name\x18\n \x01(\t\x12\x15\n\rtimezone_data\x18\x0b \x01(\x0c"\xc8\x01\n\x10SchedulePolicies\x12\x44\n\x0eoverlap_policy\x18\x01 \x01(\x0e\x32,.temporal.api.enums.v1.ScheduleOverlapPolicy\x12\x31\n\x0e\x63\x61tchup_window\x18\x02 \x01(\x0b\x32\x19.google.protobuf.Duration\x12\x18\n\x10pause_on_failure\x18\x03 \x01(\x08\x12!\n\x19keep_original_workflow_id\x18\x04 \x01(\x08"h\n\x0eScheduleAction\x12L\n\x0estart_workflow\x18\x01 \x01(\x0b\x32\x32.temporal.api.workflow.v1.NewWorkflowExecutionInfoH\x00\x42\x08\n\x06\x61\x63tion"\xc4\x01\n\x14ScheduleActionResult\x12\x31\n\rschedule_time\x18\x01 \x01(\x0b\x32\x1a.google.protobuf.Timestamp\x12/\n\x0b\x61\x63tual_time\x18\x02 \x01(\x0b\x32\x1a.google.protobuf.Timestamp\x12H\n\x15start_workflow_result\x18\x0b \x01(\x0b\x32).temporal.api.common.v1.WorkflowExecution"b\n\rScheduleState\x12\r\n\x05notes\x18\x01 \x01(\t\x12\x0e\n\x06paused\x18\x02 \x01(\x08\x12\x17\n\x0flimited_actions\x18\x03 \x01(\x08\x12\x19\n\x11remaining_actions\x18\x04 \x01(\x03"a\n\x19TriggerImmediatelyRequest\x12\x44\n\x0eoverlap_policy\x18\x01 \x01(\x0e\x32,.temporal.api.enums.v1.ScheduleOverlapPolicy"\xb5\x01\n\x0f\x42\x61\x63kfillRequest\x12.\n\nstart_time\x18\x01 \x01(\x0b\x32\x1a.google.protobuf.Timestamp\x12,\n\x08\x65nd_time\x18\x02 \x01(\x0b\x32\x1a.google.protobuf.Timestamp\x12\x44\n\x0eoverlap_policy\x18\x03 \x01(\x0e\x32,.temporal.api.enums.v1.ScheduleOverlapPolicy"\xc6\x01\n\rSchedulePatch\x12P\n\x13trigger_immediately\x18\x01 \x01(\x0b\x32\x33.temporal.api.schedule.v1.TriggerImmediatelyRequest\x12\x43\n\x10\x62\x61\x63kfill_request\x18\x02 \x03(\x0b\x32).temporal.api.schedule.v1.BackfillRequest\x12\r\n\x05pause\x18\x03 \x01(\t\x12\x0f\n\x07unpause\x18\x04 \x01(\t"\xd6\x03\n\x0cScheduleInfo\x12\x14\n\x0c\x61\x63tion_count\x18\x01 \x01(\x03\x12\x1d\n\x15missed_catchup_window\x18\x02 \x01(\x03\x12\x17\n\x0foverlap_skipped\x18\x03 \x01(\x03\x12\x16\n\x0e\x62uffer_dropped\x18\n \x01(\x03\x12\x13\n\x0b\x62uffer_size\x18\x0b \x01(\x03\x12\x44\n\x11running_workflows\x18\t \x03(\x0b\x32).temporal.api.common.v1.WorkflowExecution\x12\x46\n\x0erecent_actions\x18\x04 \x03(\x0b\x32..temporal.api.schedule.v1.ScheduleActionResult\x12\x37\n\x13\x66uture_action_times\x18\x05 \x03(\x0b\x32\x1a.google.protobuf.Timestamp\x12/\n\x0b\x63reate_time\x18\x06 \x01(\x0b\x32\x1a.google.protobuf.Timestamp\x12/\n\x0bupdate_time\x18\x07 \x01(\x0b\x32\x1a.google.protobuf.Timestamp\x12"\n\x16invalid_schedule_error\x18\x08 \x01(\tB\x02\x18\x01"\xf0\x01\n\x08Schedule\x12\x34\n\x04spec\x18\x01 \x01(\x0b\x32&.temporal.api.schedule.v1.ScheduleSpec\x12\x38\n\x06\x61\x63tion\x18\x02 \x01(\x0b\x32(.temporal.api.schedule.v1.ScheduleAction\x12<\n\x08policies\x18\x03 \x01(\x0b\x32*.temporal.api.schedule.v1.SchedulePolicies\x12\x36\n\x05state\x18\x04 \x01(\x0b\x32\'.temporal.api.schedule.v1.ScheduleState"\xa5\x02\n\x10ScheduleListInfo\x12\x34\n\x04spec\x18\x01 \x01(\x0b\x32&.temporal.api.schedule.v1.ScheduleSpec\x12;\n\rworkflow_type\x18\x02 \x01(\x0b\x32$.temporal.api.common.v1.WorkflowType\x12\r\n\x05notes\x18\x03 \x01(\t\x12\x0e\n\x06paused\x18\x04 \x01(\x08\x12\x46\n\x0erecent_actions\x18\x05 \x03(\x0b\x32..temporal.api.schedule.v1.ScheduleActionResult\x12\x37\n\x13\x66uture_action_times\x18\x06 \x03(\x0b\x32\x1a.google.protobuf.Timestamp"\xd3\x01\n\x11ScheduleListEntry\x12\x13\n\x0bschedule_id\x18\x01 \x01(\t\x12*\n\x04memo\x18\x02 \x01(\x0b\x32\x1c.temporal.api.common.v1.Memo\x12\x43\n\x11search_attributes\x18\x03 \x01(\x0b\x32(.temporal.api.common.v1.SearchAttributes\x12\x38\n\x04info\x18\x04 \x01(\x0b\x32*.temporal.api.schedule.v1.ScheduleListInfoB\x93\x01\n\x1bio.temporal.api.schedule.v1B\x0cMessageProtoP\x01Z\'go.temporal.io/api/schedule/v1;schedule\xaa\x02\x1aTemporalio.Api.Schedule.V1\xea\x02\x1dTemporalio::Api::Schedule::V1b\x06proto3'
)


_CALENDARSPEC = DESCRIPTOR.message_types_by_name["CalendarSpec"]
_RANGE = DESCRIPTOR.message_types_by_name["Range"]
_STRUCTUREDCALENDARSPEC = DESCRIPTOR.message_types_by_name["StructuredCalendarSpec"]
_INTERVALSPEC = DESCRIPTOR.message_types_by_name["IntervalSpec"]
_SCHEDULESPEC = DESCRIPTOR.message_types_by_name["ScheduleSpec"]
_SCHEDULEPOLICIES = DESCRIPTOR.message_types_by_name["SchedulePolicies"]
_SCHEDULEACTION = DESCRIPTOR.message_types_by_name["ScheduleAction"]
_SCHEDULEACTIONRESULT = DESCRIPTOR.message_types_by_name["ScheduleActionResult"]
_SCHEDULESTATE = DESCRIPTOR.message_types_by_name["ScheduleState"]
_TRIGGERIMMEDIATELYREQUEST = DESCRIPTOR.message_types_by_name[
    "TriggerImmediatelyRequest"
]
_BACKFILLREQUEST = DESCRIPTOR.message_types_by_name["BackfillRequest"]
_SCHEDULEPATCH = DESCRIPTOR.message_types_by_name["SchedulePatch"]
_SCHEDULEINFO = DESCRIPTOR.message_types_by_name["ScheduleInfo"]
_SCHEDULE = DESCRIPTOR.message_types_by_name["Schedule"]
_SCHEDULELISTINFO = DESCRIPTOR.message_types_by_name["ScheduleListInfo"]
_SCHEDULELISTENTRY = DESCRIPTOR.message_types_by_name["ScheduleListEntry"]
CalendarSpec = _reflection.GeneratedProtocolMessageType(
    "CalendarSpec",
    (_message.Message,),
    {
        "DESCRIPTOR": _CALENDARSPEC,
        "__module__": "temporal.api.schedule.v1.message_pb2"
        # @@protoc_insertion_point(class_scope:temporal.api.schedule.v1.CalendarSpec)
    },
)
_sym_db.RegisterMessage(CalendarSpec)

Range = _reflection.GeneratedProtocolMessageType(
    "Range",
    (_message.Message,),
    {
        "DESCRIPTOR": _RANGE,
        "__module__": "temporal.api.schedule.v1.message_pb2"
        # @@protoc_insertion_point(class_scope:temporal.api.schedule.v1.Range)
    },
)
_sym_db.RegisterMessage(Range)

StructuredCalendarSpec = _reflection.GeneratedProtocolMessageType(
    "StructuredCalendarSpec",
    (_message.Message,),
    {
        "DESCRIPTOR": _STRUCTUREDCALENDARSPEC,
        "__module__": "temporal.api.schedule.v1.message_pb2"
        # @@protoc_insertion_point(class_scope:temporal.api.schedule.v1.StructuredCalendarSpec)
    },
)
_sym_db.RegisterMessage(StructuredCalendarSpec)

IntervalSpec = _reflection.GeneratedProtocolMessageType(
    "IntervalSpec",
    (_message.Message,),
    {
        "DESCRIPTOR": _INTERVALSPEC,
        "__module__": "temporal.api.schedule.v1.message_pb2"
        # @@protoc_insertion_point(class_scope:temporal.api.schedule.v1.IntervalSpec)
    },
)
_sym_db.RegisterMessage(IntervalSpec)

ScheduleSpec = _reflection.GeneratedProtocolMessageType(
    "ScheduleSpec",
    (_message.Message,),
    {
        "DESCRIPTOR": _SCHEDULESPEC,
        "__module__": "temporal.api.schedule.v1.message_pb2"
        # @@protoc_insertion_point(class_scope:temporal.api.schedule.v1.ScheduleSpec)
    },
)
_sym_db.RegisterMessage(ScheduleSpec)

SchedulePolicies = _reflection.GeneratedProtocolMessageType(
    "SchedulePolicies",
    (_message.Message,),
    {
        "DESCRIPTOR": _SCHEDULEPOLICIES,
        "__module__": "temporal.api.schedule.v1.message_pb2"
        # @@protoc_insertion_point(class_scope:temporal.api.schedule.v1.SchedulePolicies)
    },
)
_sym_db.RegisterMessage(SchedulePolicies)

ScheduleAction = _reflection.GeneratedProtocolMessageType(
    "ScheduleAction",
    (_message.Message,),
    {
        "DESCRIPTOR": _SCHEDULEACTION,
        "__module__": "temporal.api.schedule.v1.message_pb2"
        # @@protoc_insertion_point(class_scope:temporal.api.schedule.v1.ScheduleAction)
    },
)
_sym_db.RegisterMessage(ScheduleAction)

ScheduleActionResult = _reflection.GeneratedProtocolMessageType(
    "ScheduleActionResult",
    (_message.Message,),
    {
        "DESCRIPTOR": _SCHEDULEACTIONRESULT,
        "__module__": "temporal.api.schedule.v1.message_pb2"
        # @@protoc_insertion_point(class_scope:temporal.api.schedule.v1.ScheduleActionResult)
    },
)
_sym_db.RegisterMessage(ScheduleActionResult)

ScheduleState = _reflection.GeneratedProtocolMessageType(
    "ScheduleState",
    (_message.Message,),
    {
        "DESCRIPTOR": _SCHEDULESTATE,
        "__module__": "temporal.api.schedule.v1.message_pb2"
        # @@protoc_insertion_point(class_scope:temporal.api.schedule.v1.ScheduleState)
    },
)
_sym_db.RegisterMessage(ScheduleState)

TriggerImmediatelyRequest = _reflection.GeneratedProtocolMessageType(
    "TriggerImmediatelyRequest",
    (_message.Message,),
    {
        "DESCRIPTOR": _TRIGGERIMMEDIATELYREQUEST,
        "__module__": "temporal.api.schedule.v1.message_pb2"
        # @@protoc_insertion_point(class_scope:temporal.api.schedule.v1.TriggerImmediatelyRequest)
    },
)
_sym_db.RegisterMessage(TriggerImmediatelyRequest)

BackfillRequest = _reflection.GeneratedProtocolMessageType(
    "BackfillRequest",
    (_message.Message,),
    {
        "DESCRIPTOR": _BACKFILLREQUEST,
        "__module__": "temporal.api.schedule.v1.message_pb2"
        # @@protoc_insertion_point(class_scope:temporal.api.schedule.v1.BackfillRequest)
    },
)
_sym_db.RegisterMessage(BackfillRequest)

SchedulePatch = _reflection.GeneratedProtocolMessageType(
    "SchedulePatch",
    (_message.Message,),
    {
        "DESCRIPTOR": _SCHEDULEPATCH,
        "__module__": "temporal.api.schedule.v1.message_pb2"
        # @@protoc_insertion_point(class_scope:temporal.api.schedule.v1.SchedulePatch)
    },
)
_sym_db.RegisterMessage(SchedulePatch)

ScheduleInfo = _reflection.GeneratedProtocolMessageType(
    "ScheduleInfo",
    (_message.Message,),
    {
        "DESCRIPTOR": _SCHEDULEINFO,
        "__module__": "temporal.api.schedule.v1.message_pb2"
        # @@protoc_insertion_point(class_scope:temporal.api.schedule.v1.ScheduleInfo)
    },
)
_sym_db.RegisterMessage(ScheduleInfo)

Schedule = _reflection.GeneratedProtocolMessageType(
    "Schedule",
    (_message.Message,),
    {
        "DESCRIPTOR": _SCHEDULE,
        "__module__": "temporal.api.schedule.v1.message_pb2"
        # @@protoc_insertion_point(class_scope:temporal.api.schedule.v1.Schedule)
    },
)
_sym_db.RegisterMessage(Schedule)

ScheduleListInfo = _reflection.GeneratedProtocolMessageType(
    "ScheduleListInfo",
    (_message.Message,),
    {
        "DESCRIPTOR": _SCHEDULELISTINFO,
        "__module__": "temporal.api.schedule.v1.message_pb2"
        # @@protoc_insertion_point(class_scope:temporal.api.schedule.v1.ScheduleListInfo)
    },
)
_sym_db.RegisterMessage(ScheduleListInfo)

ScheduleListEntry = _reflection.GeneratedProtocolMessageType(
    "ScheduleListEntry",
    (_message.Message,),
    {
        "DESCRIPTOR": _SCHEDULELISTENTRY,
        "__module__": "temporal.api.schedule.v1.message_pb2"
        # @@protoc_insertion_point(class_scope:temporal.api.schedule.v1.ScheduleListEntry)
    },
)
_sym_db.RegisterMessage(ScheduleListEntry)

if _descriptor._USE_C_DESCRIPTORS == False:
    DESCRIPTOR._options = None
    DESCRIPTOR._serialized_options = b"\n\033io.temporal.api.schedule.v1B\014MessageProtoP\001Z'go.temporal.io/api/schedule/v1;schedule\252\002\032Temporalio.Api.Schedule.V1\352\002\035Temporalio::Api::Schedule::V1"
    _SCHEDULESPEC.fields_by_name["exclude_calendar"]._options = None
    _SCHEDULESPEC.fields_by_name["exclude_calendar"]._serialized_options = b"\030\001"
    _SCHEDULEINFO.fields_by_name["invalid_schedule_error"]._options = None
    _SCHEDULEINFO.fields_by_name[
        "invalid_schedule_error"
    ]._serialized_options = b"\030\001"
    _CALENDARSPEC._serialized_start = 250
    _CALENDARSPEC._serialized_end = 399
    _RANGE._serialized_start = 401
    _RANGE._serialized_end = 450
    _STRUCTUREDCALENDARSPEC._serialized_start = 453
    _STRUCTUREDCALENDARSPEC._serialized_end = 843
    _INTERVALSPEC._serialized_start = 845
    _INTERVALSPEC._serialized_end = 946
    _SCHEDULESPEC._serialized_start = 949
    _SCHEDULESPEC._serialized_end = 1519
    _SCHEDULEPOLICIES._serialized_start = 1522
    _SCHEDULEPOLICIES._serialized_end = 1722
    _SCHEDULEACTION._serialized_start = 1724
    _SCHEDULEACTION._serialized_end = 1828
    _SCHEDULEACTIONRESULT._serialized_start = 1831
    _SCHEDULEACTIONRESULT._serialized_end = 2027
    _SCHEDULESTATE._serialized_start = 2029
    _SCHEDULESTATE._serialized_end = 2127
    _TRIGGERIMMEDIATELYREQUEST._serialized_start = 2129
    _TRIGGERIMMEDIATELYREQUEST._serialized_end = 2226
    _BACKFILLREQUEST._serialized_start = 2229
    _BACKFILLREQUEST._serialized_end = 2410
    _SCHEDULEPATCH._serialized_start = 2413
    _SCHEDULEPATCH._serialized_end = 2611
    _SCHEDULEINFO._serialized_start = 2614
    _SCHEDULEINFO._serialized_end = 3084
    _SCHEDULE._serialized_start = 3087
    _SCHEDULE._serialized_end = 3327
    _SCHEDULELISTINFO._serialized_start = 3330
    _SCHEDULELISTINFO._serialized_end = 3623
    _SCHEDULELISTENTRY._serialized_start = 3626
    _SCHEDULELISTENTRY._serialized_end = 3837
# @@protoc_insertion_point(module_scope)
