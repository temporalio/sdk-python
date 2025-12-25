"""Prototype 2: Validate write capture via CONFIG_KEY_SEND.

Technical Concern:
    The proposal assumes nodes write state via CONFIG_KEY_SEND callback,
    and we can capture writes by injecting our own callback.

Questions to Answer:
    1. Does CONFIG_KEY_SEND exist in the config?
    2. What is the callback signature?
    3. What format are writes in? [(channel, value), ...]?
    4. Do all node types (regular, ToolNode) use this mechanism?
    5. Can we inject our callback and capture all writes?

Status: NOT IMPLEMENTED - placeholder for commit 3
"""

# Implementation will be added in commit 3
