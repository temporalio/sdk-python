- `contrib.google_adk_agents`: agents with an `output_schema` no longer fail every workflow task
  when calling the model. The schema type is now sent to the model activity as its JSON schema.
  Custom Pydantic schema generation is preserved.
  Integer-valued output enums are normalized to strings to match Google GenAI.
