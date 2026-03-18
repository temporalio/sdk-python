# Workspace Sync for Temporal Activities

Sync a local directory with remote storage before and after a Temporal activity. Enables file-based activities to work across distributed workers where disk is not shared.

## Problem

Temporal activities that read/write files on local disk break when you scale to multiple worker instances — each worker has its own disk. This module solves that by syncing a remote storage location to a local temp directory before the activity runs, and pushing changes back after.

## Install

```bash
pip install temporalio[workdir]

# With a specific cloud backend:
pip install temporalio[workdir] gcsfs    # Google Cloud Storage
pip install temporalio[workdir] s3fs     # Amazon S3
pip install temporalio[workdir] adlfs    # Azure Blob Storage
```

## Usage

### As a context manager (generic, works anywhere)

```python
from temporalio.contrib.workdir import Workspace

async with Workspace("gs://my-bucket/pipeline/component-x") as ws:
    # ws.path is a local Path — read and write files normally
    data = json.loads((ws.path / "component.json").read_text())
    (ws.path / "result.csv").write_text("col1,col2\nval1,val2")
    # On clean exit: local dir is archived and uploaded
    # On exception: no upload (remote state unchanged)
```

### As a Temporal activity decorator

```python
from temporalio import activity
from temporalio.contrib.workdir import workspace, get_workspace_path

@workspace("gs://my-bucket/{workflow_id}/{activity_type}")
@activity.defn
async def extract(input: ExtractInput) -> ExtractOutput:
    ws = get_workspace_path()
    # Template vars resolved from activity.info()
    source = (ws / "source.json").read_text()
    (ws / "output.csv").write_text(process(source))
    return ExtractOutput(success=True)
```

### Custom template variables

```python
@workspace(
    "gs://my-bucket/{workflow_id}/components/{component}",
    key_fn=lambda input: {"component": input.component_name},
)
@activity.defn
async def register(input: RegisterInput) -> RegisterOutput:
    ws = get_workspace_path()
    ...
```

## How It Works

1. **Pull**: On entry, downloads `{remote_url}.tar.gz` and unpacks to a temp directory
2. **Execute**: Your activity reads/writes files in the local directory
3. **Push**: On clean exit, packs the directory into `tar.gz` and uploads

If the archive doesn't exist yet (first run), the local directory starts empty. If the activity raises an exception, no push happens — remote state is untouched.

## Storage Backends

Any backend supported by [fsspec](https://filesystem-spec.readthedocs.io/):

| Scheme | Backend | Extra package |
|--------|---------|--------------|
| `gs://` | Google Cloud Storage | `gcsfs` |
| `s3://` | Amazon S3 | `s3fs` |
| `az://` | Azure Blob Storage | `adlfs` |
| `file://` | Local filesystem | (none) |
| `memory://` | In-memory (testing) | (none) |

Pass backend-specific options as keyword arguments:

```python
Workspace("gs://bucket/key", project="my-gcp-project", token="cloud")
```
