Exclude computed fields when the Pydantic payload converter serializes to JSON, so models using computed fields round-trip even with extra="forbid". Requires pydantic>=2.11.
