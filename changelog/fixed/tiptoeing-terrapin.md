Send a request ID when terminating a standalone activity, so a retried `ActivityHandle.terminate()` call no longer fails after the first attempt already terminated the activity.
