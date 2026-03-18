# Activity Cache for Temporal

Content-addressed activity memoization with shared remote storage. Same inputs = skip execution, return cached result. Makes workflows idempotent across retries, re-runs, and distributed workers.

## Problem

Temporal activities are not idempotent by default. If a workflow retries or re-runs, activities execute again — even when their inputs haven't changed. For expensive activities (AI inference, API calls, data processing), this wastes time and money. This module caches activity results so repeated calls with the same inputs return instantly.

## Install

```bash
pip install temporalio[activity-cache]

# With a specific cloud backend:
pip install temporalio[activity-cache] gcsfs    # Google Cloud Storage
pip install temporalio[activity-cache] s3fs     # Amazon S3
```

## Usage

### As a decorator (explicit, per-activity)

```python
from temporalio import activity
from temporalio.contrib.activity_cache import cached

@cached("gs://my-bucket/cache", ttl=timedelta(days=90))
@activity.defn
async def extract(input: ExtractInput) -> ExtractOutput:
    ...  # Only runs on cache miss
```

### With custom key function

By default, all arguments are included in the cache key. Use `key_fn` to select specific args:

```python
@cached(
    "gs://my-bucket/cache",
    key_fn=lambda input: {
        "component": input.component_name,
        "content_hash": input.content_hash,
    },
)
@activity.defn
async def extract(input: ExtractInput) -> ExtractOutput:
    # Cache key only considers component + content_hash
    # Changes to input.timestamp or input.run_id won't bust the cache
    ...
```

### As an interceptor (transparent, all activities)

```python
from temporalio.contrib.activity_cache import CachingInterceptor, no_cache

worker = Worker(
    client,
    task_queue="my-queue",
    activities=[extract, register, verify, commit],
    interceptors=[CachingInterceptor("gs://my-bucket/cache", ttl=timedelta(days=90))],
)

# Opt out specific activities:
@no_cache
@activity.defn
async def commit(input: CommitInput) -> CommitOutput:
    ...  # Always executes, never cached
```

## How It Works

1. **Key computation**: SHA256 hash of function name + serialized arguments
2. **Cache check**: Look up `{base_url}/{fn_name}/{key}.pkl` in remote store
3. **Cache hit**: Unpickle and return the stored result — activity body never runs
4. **Cache miss**: Execute activity, pickle result, upload to remote store

## Serialization

Arguments are serialized deterministically for cache key computation:

| Type | Serialization |
|------|--------------|
| `str`, `int`, `float`, `bool`, `None` | Pass-through |
| `bytes` | SHA256 hash (first 16 chars) |
| `Path` (file) | SHA256 of file content (first 16 chars) |
| Pydantic `BaseModel` | `.model_dump()` (recursive) |
| `dataclass` | `dataclasses.asdict()` (recursive) |
| `dict` | Sorted keys, recursive values |
| `list`, `tuple` | Recursive elements |

Register custom serializers for domain types:

```python
from temporalio.contrib.activity_cache import register_serializer

register_serializer(MyType, lambda obj: {"id": obj.id, "version": obj.version})
```

## Storage Backends

Any backend supported by [fsspec](https://filesystem-spec.readthedocs.io/):

| Scheme | Backend | Extra package |
|--------|---------|--------------|
| `gs://` | Google Cloud Storage | `gcsfs` |
| `s3://` | Amazon S3 | `s3fs` |
| `az://` | Azure Blob Storage | `adlfs` |
| `file://` | Local filesystem | (none) |
| `memory://` | In-memory (testing) | (none) |
