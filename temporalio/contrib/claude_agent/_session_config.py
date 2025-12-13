"""Configuration for Claude Agent sessions."""

from typing import Any

from pydantic import BaseModel, Field


class ClaudeSessionConfig(BaseModel):
    """Serializable configuration for Claude Agent sessions.

    This contains only the parameters that can be serialized and passed
    through Temporal workflows. Callbacks, hooks, and file objects are
    not included and should be configured at the provider level.
    """

    # Basic options
    system_prompt: str | None = None
    max_turns: int | None = None
    max_budget_usd: float | None = None
    model: str | None = None
    fallback_model: str | None = None

    # Tools configuration
    allowed_tools: list[str] = Field(default_factory=list)
    disallowed_tools: list[str] = Field(default_factory=list)

    # Permission mode
    permission_mode: str | None = None  # 'default', 'acceptEdits', 'plan', 'bypassPermissions'

    # Working directory and environment
    cwd: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    add_dirs: list[str] = Field(default_factory=list)

    # Session control
    continue_conversation: bool = False
    resume: str | None = None
    fork_session: bool = False

    # Settings
    settings: str | None = None
    setting_sources: list[str] | None = None  # 'user', 'project', 'local'

    # CLI configuration
    cli_path: str | None = None
    extra_args: dict[str, str | None] = Field(default_factory=dict)

    # Advanced options
    include_partial_messages: bool = False
    max_thinking_tokens: int | None = None
    enable_file_checkpointing: bool = False
    betas: list[str] = Field(default_factory=list)

    # User identification
    user: str | None = None

    def to_claude_options(self) -> Any:
        """Convert to ClaudeAgentOptions.

        Returns:
            ClaudeAgentOptions instance with values from this config
        """
        from claude_agent_sdk import ClaudeAgentOptions

        # Convert to dict, excluding unset fields
        config_dict = self.model_dump(exclude_unset=True)

        # Create ClaudeAgentOptions with our values
        return ClaudeAgentOptions(**config_dict)

    class Config:
        """Pydantic configuration."""

        # Allow for future expansion
        extra = "forbid"
