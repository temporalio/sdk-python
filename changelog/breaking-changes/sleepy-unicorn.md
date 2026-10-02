- The OpenAI Agents integration has moved to the independently versioned
  [`temporalio-openai-agents`](https://pypi.org/project/temporalio-openai-agents/)
  package. The existing `temporalio[openai-agents]` extra now installs that
  package, and the old public `temporalio.contrib.openai_agents` imports
  remain available at runtime and retain their static type information.
  New code should depend on `temporalio-openai-agents` directly and import
  `temporalio.openai_agents`.
