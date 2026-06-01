import * as common from "@temporalio/common";
import type { google, temporal } from "@temporalio/proto";
import * as workflow from "@temporalio/workflow";
import type Long from "long";

function int64ToNumber(value: Long | number | string | object | null | undefined): number {
  if (value == null) {
    return 0;
  }
  if (typeof value === "number") {
    return value;
  }
  if (typeof value === "string") {
    return Number(value);
  }
  if ("toNumber" in value && typeof value.toNumber === "function") {
    return value.toNumber();
  }
  if ("low" in value && "high" in value) {
    const longValue = value as { low: number; high: number; unsigned?: boolean };
    const low = longValue.low >>> 0;
    return longValue.high * 4_294_967_296 + low;
  }
  throw new TypeError("unsupported int64 value");
}

function durationToMillis(
  proto: google.protobuf.IDuration | null | undefined,
): number | undefined {
  if (proto == null) {
    return undefined;
  }
  return int64ToNumber(proto.seconds) * 1000 + Math.floor((proto.nanos ?? 0) / 1_000_000);
}

export function retryPolicyFromProto(
  proto: temporal.api.common.v1.IRetryPolicy,
): common.RetryPolicy {
  return {
    backoffCoefficient: proto.backoffCoefficient ?? undefined,
    maximumAttempts: proto.maximumAttempts ?? undefined,
    maximumInterval: durationToMillis(proto.maximumInterval),
    initialInterval: durationToMillis(proto.initialInterval),
    nonRetryableErrorTypes: proto.nonRetryableErrorTypes ?? undefined,
  };
}

export function retryPolicyToProto(
  retryPolicy: common.RetryPolicy,
): temporal.api.common.v1.IRetryPolicy {
  return common.compileRetryPolicy(retryPolicy);
}

export function workflowTypeFromProto(
  proto: temporal.api.common.v1.IWorkflowType,
): string | common.Workflow {
  return proto.name ?? "";
}

export function workflowTypeToProto(
  workflowType: string | common.Workflow,
): temporal.api.common.v1.IWorkflowType {
  return { name: workflowFunctionName(workflowType) };
}

export function workflowFunctionName(
  value: string | common.Workflow,
): string {
  return typeof value === "string" ? value : common.extractWorkflowType(value);
}

export function signalFunctionToProto(
  value: string | workflow.SignalDefinition<any[]>,
): string {
  return typeof value === "string" ? value : value.name;
}

export function taskQueueFromProto(
  proto: temporal.api.taskqueue.v1.ITaskQueue,
): string {
  return proto.name ?? "";
}

export function taskQueueToProto(
  taskQueue: string,
): temporal.api.taskqueue.v1.ITaskQueue {
  return { name: taskQueue };
}

export function workflowNamespace(): string {
  return workflow.workflowInfo().namespace;
}

export function payloadFromProto(
  payload: temporal.api.common.v1.IPayload,
): common.Payload {
  return payload;
}

export function payloadToProto(
  payload: common.Payload,
): temporal.api.common.v1.IPayload {
  return payload;
}

function configuredPayloadConverter(): common.PayloadConverter {
  const activator = (
    globalThis as typeof globalThis & {
      __TEMPORAL_ACTIVATOR__?: {
        payloadConverter?: common.PayloadConverter;
      };
    }
  ).__TEMPORAL_ACTIVATOR__;
  if (activator?.payloadConverter == null) {
    throw new Error("payload converter is unavailable outside workflow context");
  }
  return activator.payloadConverter;
}

export function memoFromProto(
  proto: temporal.api.common.v1.IMemo,
): Record<string, unknown> {
  return (
    common.mapFromPayloads(
      configuredPayloadConverter(),
      proto.fields ?? undefined,
    ) ?? {}
  );
}

export function memoToProto(
  memo: Record<string, unknown>,
): temporal.api.common.v1.IMemo {
  return {
    fields: common.mapToPayloads(configuredPayloadConverter(), memo),
  };
}

export function durationFromProto(
  proto: google.protobuf.IDuration,
): common.Duration {
  return durationToMillis(proto)!;
}

export function durationToProto(
  duration: common.Duration,
): google.protobuf.IDuration {
  return common.msToTs(duration);
}

function typedSearchAttributePayload(
  value: unknown,
  type: common.SearchAttributeType,
): common.Payload {
  const payload = configuredPayloadConverter().toPayload(value);
  payload.metadata ??= {};
  payload.metadata.type = common.u8(
    common.TypedSearchAttributes.toMetadataType(type),
  );
  return payload;
}

function isValidSearchAttributeValue(
  type: common.SearchAttributeType,
  value: unknown,
): boolean {
  switch (type) {
    case common.SearchAttributeType.TEXT:
    case common.SearchAttributeType.KEYWORD:
      return typeof value === "string";
    case common.SearchAttributeType.INT:
      return Number.isInteger(value);
    case common.SearchAttributeType.DOUBLE:
      return typeof value === "number";
    case common.SearchAttributeType.BOOL:
      return typeof value === "boolean";
    case common.SearchAttributeType.DATETIME:
      return value instanceof Date;
    case common.SearchAttributeType.KEYWORD_LIST:
      return (
        Array.isArray(value) &&
        value.every((item) => typeof item === "string")
      );
    default:
      return false;
  }
}

function typedSearchAttributePairFromPayload(
  name: string,
  payload: common.Payload,
): common.SearchAttributePair | undefined {
  const metadataType = payload.metadata?.type;
  if (metadataType == null) {
    return undefined;
  }
  const type = common.TypedSearchAttributes.toSearchAttributeType(
    common.str(metadataType),
  );
  if (type == null) {
    return undefined;
  }
  let value: unknown = configuredPayloadConverter().fromPayload(payload);
  if (
    type !== common.SearchAttributeType.KEYWORD_LIST &&
    Array.isArray(value)
  ) {
    if (value.length !== 1) {
      return undefined;
    }
    value = value[0];
  }
  if (type === common.SearchAttributeType.DATETIME && value != null) {
    value = new Date(value as string);
  }
  if (!isValidSearchAttributeValue(type, value)) {
    return undefined;
  }
  return {
    key: { name, type },
    value,
  } as common.SearchAttributePair;
}

export function searchAttributesFromProto(
  proto: temporal.api.common.v1.ISearchAttributes,
): common.TypedSearchAttributes {
  const indexedFields = proto.indexedFields ?? {};
  const typedPairs: common.SearchAttributePair[] = [];
  for (const [name, payload] of Object.entries(indexedFields)) {
    const pair = typedSearchAttributePairFromPayload(name, payload);
    if (pair == null) {
      throw new TypeError(
        `search attribute ${name} cannot be decoded as a typed search attribute`,
      );
    }
    typedPairs.push(pair);
  }
  return new common.TypedSearchAttributes(typedPairs);
}

export function searchAttributesToProto(
  searchAttributes: common.TypedSearchAttributes,
): temporal.api.common.v1.ISearchAttributes {
  return {
    indexedFields: Object.fromEntries(
      searchAttributes.getAll().map((pair): [string, common.Payload] => [
        pair.key.name,
        typedSearchAttributePayload(pair.value, pair.key.type),
      ]),
    ),
  };
}

export function priorityFromProto(
  proto: temporal.api.common.v1.IPriority,
): common.Priority {
  return common.decodePriority(proto);
}

export function priorityToProto(
  priority: common.Priority,
): temporal.api.common.v1.IPriority {
  return common.compilePriority(priority);
}

const VERSIONING_BEHAVIOR_PINNED = 1;
const VERSIONING_BEHAVIOR_AUTO_UPGRADE = 2;
const PINNED_OVERRIDE_BEHAVIOR_PINNED = 1;

export function versioningOverrideFromProto(
  proto: temporal.api.workflow.v1.IVersioningOverride,
): common.VersioningOverride | undefined {
  if (
    proto.autoUpgrade ||
    proto.behavior === VERSIONING_BEHAVIOR_AUTO_UPGRADE
  ) {
    return "AUTO_UPGRADE";
  }
  const pinnedVersion = proto.pinned?.version;
  if (pinnedVersion?.deploymentName != null && pinnedVersion.buildId != null) {
    return {
      pinnedTo: {
        deploymentName: pinnedVersion.deploymentName,
        buildId: pinnedVersion.buildId,
      },
    };
  }
  if (
    proto.deployment?.seriesName != null &&
    proto.deployment.buildId != null
  ) {
    return {
      pinnedTo: {
        deploymentName: proto.deployment.seriesName,
        buildId: proto.deployment.buildId,
      },
    };
  }
  return undefined;
}

export function versioningOverrideToProto(
  versioningOverride: common.VersioningOverride,
): temporal.api.workflow.v1.IVersioningOverride {
  if (versioningOverride === "AUTO_UPGRADE") {
    return {
      behavior: VERSIONING_BEHAVIOR_AUTO_UPGRADE,
      autoUpgrade: true,
    };
  }
  return {
    behavior: VERSIONING_BEHAVIOR_PINNED,
    pinnedVersion: common.toCanonicalString(versioningOverride.pinnedTo),
    pinned: {
      behavior: PINNED_OVERRIDE_BEHAVIOR_PINNED,
      version: {
        deploymentName: versioningOverride.pinnedTo.deploymentName,
        buildId: versioningOverride.pinnedTo.buildId,
      },
    },
  };
}

export function workflowIdReusePolicyFromProto(
  policy: temporal.api.enums.v1.WorkflowIdReusePolicy,
): common.WorkflowIdReusePolicy | undefined {
  return common.decodeWorkflowIdReusePolicy(policy);
}

export function workflowIdReusePolicyToProto(
  policy: common.WorkflowIdReusePolicy,
): temporal.api.enums.v1.WorkflowIdReusePolicy | undefined {
  return common.encodeWorkflowIdReusePolicy(policy);
}

export function workflowIdConflictPolicyFromProto(
  policy: temporal.api.enums.v1.WorkflowIdConflictPolicy,
): common.WorkflowIdConflictPolicy | undefined {
  return common.decodeWorkflowIdConflictPolicy(policy);
}

export function workflowIdConflictPolicyToProto(
  policy: common.WorkflowIdConflictPolicy,
): temporal.api.enums.v1.WorkflowIdConflictPolicy | undefined {
  return common.encodeWorkflowIdConflictPolicy(policy);
}
