def make_nexus_endpoint_name(task_queue: str) -> str:
    # Create endpoints for different task queues without name collisions.
    return f"nexus-endpoint-{task_queue}"
