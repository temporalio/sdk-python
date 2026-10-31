Keep an empty TLS table when `ClientConfigProfile` and `ClientConfig` are converted to and from dictionaries, so a profile that enables TLS with default settings does not lose it.
