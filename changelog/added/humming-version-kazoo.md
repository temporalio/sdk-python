Client and worker plugins have a new `Plugin.version()` method. Worker plugins report it in worker heartbeats; override it or pass `version` to `SimplePlugin`.
