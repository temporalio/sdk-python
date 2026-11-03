use pyo3::prelude::*;
use pyo3::{
    exceptions::PyRuntimeError,
    types::{PyBytes, PyDict, PyList},
};
use std::collections::HashMap;
use temporalio_common::envconfig::{
    load_client_config as core_load_client_config,
    load_client_config_profile as core_load_client_config_profile, toml,
    ClientConfig as CoreClientConfig, ClientConfigCodec,
    ClientConfigProfile as CoreClientConfigProfile, ClientConfigTLS as CoreClientConfigTLS,
    DataSource, LoadClientConfigOptions, LoadClientConfigProfileOptions,
};

pyo3::create_exception!(temporal_sdk_bridge, ConfigError, PyRuntimeError);

fn data_source_to_dict(py: Python, ds: &DataSource) -> PyResult<Py<PyAny>> {
    let dict = PyDict::new(py);
    match ds {
        DataSource::Path(p) => dict.set_item("path", p)?,
        DataSource::Data(d) => dict.set_item("data", PyBytes::new(py, d))?,
        _ => {
            return Err(PyRuntimeError::new_err(
                "unsupported configuration data source",
            ))
        }
    };
    Ok(dict.into())
}

fn tls_to_dict(py: Python, tls: &CoreClientConfigTLS) -> PyResult<Py<PyAny>> {
    let dict = PyDict::new(py);
    dict.set_item("disabled", tls.disabled)?;
    if let Some(v) = &tls.client_cert {
        dict.set_item("client_cert", data_source_to_dict(py, v)?)?;
    }
    if let Some(v) = &tls.client_key {
        dict.set_item("client_key", data_source_to_dict(py, v)?)?;
    }
    if let Some(v) = &tls.server_ca_cert {
        dict.set_item("server_ca_cert", data_source_to_dict(py, v)?)?;
    }
    if let Some(v) = &tls.server_name {
        dict.set_item("server_name", v)?;
    }
    dict.set_item("disable_host_verification", tls.disable_host_verification)?;
    Ok(dict.into())
}

fn codec_to_dict(py: Python, codec: &ClientConfigCodec) -> PyResult<Py<PyAny>> {
    let dict = PyDict::new(py);
    if let Some(v) = &codec.endpoint {
        dict.set_item("endpoint", v)?;
    }
    if let Some(v) = &codec.auth {
        dict.set_item("auth", v)?;
    }
    Ok(dict.into())
}

fn profile_to_dict(py: Python, profile: &CoreClientConfigProfile) -> PyResult<Py<PyAny>> {
    let dict = PyDict::new(py);
    if let Some(v) = &profile.address {
        dict.set_item("address", v)?;
    }
    if let Some(v) = &profile.namespace {
        dict.set_item("namespace", v)?;
    }
    if let Some(v) = &profile.api_key {
        dict.set_item("api_key", v)?;
    }
    if let Some(tls) = &profile.tls {
        dict.set_item("tls", tls_to_dict(py, tls)?)?;
    }
    if let Some(codec) = &profile.codec {
        dict.set_item("codec", codec_to_dict(py, codec)?)?;
    }
    if !profile.grpc_meta.is_empty() {
        dict.set_item("grpc_meta", &profile.grpc_meta)?;
    }
    if !profile.ext.is_empty() {
        let ext = PyDict::new(py);
        for (name, table) in &profile.ext {
            ext.set_item(name, toml_table_to_py(py, table)?)?;
        }
        dict.set_item("ext", ext)?;
    }
    Ok(dict.into())
}

fn toml_table_to_py(py: Python, table: &toml::Table) -> PyResult<Py<PyAny>> {
    let dict = PyDict::new(py);
    for (k, v) in table {
        dict.set_item(k, toml_value_to_py(py, v)?)?;
    }
    Ok(dict.into())
}

// Values are converted the same way as by Python's tomllib.
fn toml_value_to_py(py: Python, value: &toml::Value) -> PyResult<Py<PyAny>> {
    Ok(match value {
        toml::Value::String(s) => s.into_pyobject(py)?.into_any().unbind(),
        toml::Value::Integer(i) => i.into_pyobject(py)?.into_any().unbind(),
        toml::Value::Float(f) => f.into_pyobject(py)?.into_any().unbind(),
        toml::Value::Boolean(b) => b.into_pyobject(py)?.to_owned().into_any().unbind(),
        toml::Value::Datetime(dt) => toml_datetime_to_py(py, dt)?,
        toml::Value::Array(a) => {
            let list = PyList::empty(py);
            for v in a {
                list.append(toml_value_to_py(py, v)?)?;
            }
            list.into_any().unbind()
        }
        toml::Value::Table(t) => toml_table_to_py(py, t)?,
    })
}

// Offset datetimes become aware datetimes, local datetimes become naive datetimes, local dates
// become dates, and local times become times. Fractional seconds are truncated to microseconds.
// Built via the datetime module since the datetime C API isn't part of the limited API.
fn toml_datetime_to_py(py: Python, dt: &toml::value::Datetime) -> PyResult<Py<PyAny>> {
    let datetime = py.import("datetime")?;
    let time = dt.time.map(|t| {
        (
            t.hour,
            t.minute,
            t.second.unwrap_or(0),
            t.nanosecond.unwrap_or(0) / 1000,
        )
    });
    let obj = match (dt.date, time) {
        (Some(d), Some((hour, minute, second, micros))) => {
            let tz = match dt.offset {
                None => py.None().into_bound(py),
                Some(toml::value::Offset::Z) => datetime.getattr("timezone")?.getattr("utc")?,
                Some(toml::value::Offset::Custom { minutes }) => {
                    let delta = datetime
                        .getattr("timedelta")?
                        .call1((0, i32::from(minutes) * 60))?;
                    datetime.getattr("timezone")?.call1((delta,))?
                }
            };
            datetime
                .getattr("datetime")?
                .call1((d.year, d.month, d.day, hour, minute, second, micros, tz))?
        }
        (Some(d), None) => datetime.getattr("date")?.call1((d.year, d.month, d.day))?,
        (None, Some((hour, minute, second, micros))) => datetime
            .getattr("time")?
            .call1((hour, minute, second, micros))?,
        (None, None) => return Err(PyRuntimeError::new_err("empty TOML datetime")),
    };
    Ok(obj.unbind())
}

fn core_config_to_dict(py: Python, core_config: &CoreClientConfig) -> PyResult<Py<PyAny>> {
    let profiles_dict = PyDict::new(py);
    for (name, profile) in &core_config.profiles {
        let connect_dict = profile_to_dict(py, profile)?;
        profiles_dict.set_item(name, connect_dict)?;
    }
    Ok(profiles_dict.into())
}

fn load_client_config_inner(
    py: Python,
    config_source: Option<DataSource>,
    config_file_strict: bool,
    env_vars: Option<HashMap<String, String>>,
) -> PyResult<Py<PyAny>> {
    let options = LoadClientConfigOptions::builder()
        .maybe_config_source(config_source)
        .config_file_strict(config_file_strict)
        .build();
    let core_config = core_load_client_config(options, env_vars.as_ref())
        .map_err(|e| ConfigError::new_err(format!("{e}")))?;

    core_config_to_dict(py, &core_config)
}

fn load_client_connect_config_inner(
    py: Python,
    config_source: Option<DataSource>,
    profile: Option<String>,
    disable_file: bool,
    disable_env: bool,
    config_file_strict: bool,
    env_vars: Option<HashMap<String, String>>,
) -> PyResult<Py<PyAny>> {
    let options = LoadClientConfigProfileOptions::builder()
        .maybe_config_source(config_source)
        .maybe_config_file_profile(profile)
        .config_file_strict(config_file_strict)
        .disable_file(disable_file)
        .disable_env(disable_env)
        .build();

    let profile = core_load_client_config_profile(options, env_vars.as_ref())
        .map_err(|e| ConfigError::new_err(format!("{e}")))?;

    profile_to_dict(py, &profile)
}

#[pyfunction]
#[pyo3(signature = (path, data, config_file_strict, env_vars = None))]
pub fn load_client_config(
    py: Python,
    path: Option<String>,
    data: Option<Vec<u8>>,
    config_file_strict: bool,
    env_vars: Option<HashMap<String, String>>,
) -> PyResult<Py<PyAny>> {
    let config_source = match (path, data) {
        (Some(p), None) => Some(DataSource::Path(p)),
        (None, Some(d)) => Some(DataSource::Data(d)),
        (None, None) => None,
        (Some(_), Some(_)) => {
            return Err(ConfigError::new_err(
                "Cannot specify both path and data for config source",
            ))
        }
    };
    load_client_config_inner(py, config_source, config_file_strict, env_vars)
}

#[pyfunction]
#[pyo3(signature = (profile, path, data, disable_file, disable_env, config_file_strict, env_vars = None))]
#[allow(clippy::too_many_arguments)]
pub fn load_client_connect_config(
    py: Python,
    profile: Option<String>,
    path: Option<String>,
    data: Option<Vec<u8>>,
    disable_file: bool,
    disable_env: bool,
    config_file_strict: bool,
    env_vars: Option<HashMap<String, String>>,
) -> PyResult<Py<PyAny>> {
    let config_source = match (path, data) {
        (Some(p), None) => Some(DataSource::Path(p)),
        (None, Some(d)) => Some(DataSource::Data(d)),
        (None, None) => None,
        (Some(_), Some(_)) => {
            return Err(ConfigError::new_err(
                "Cannot specify both path and data for config source",
            ))
        }
    };
    load_client_connect_config_inner(
        py,
        config_source,
        profile,
        disable_file,
        disable_env,
        config_file_strict,
        env_vars,
    )
}
