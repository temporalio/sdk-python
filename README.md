# Temporal Python SDK

### Local development enviroment

- Install the system dependencies

  - Python >=3.7
  - [pipx](https://github.com/pypa/pipx#install-pipx)
  - [`poetry`](https://github.com/python-poetry/poetry) `pipx install poetry`
  - [`poe`](https://github.com/nat-n/poethepoet) `pipx install poethepoet`

- Install the package dependencies

  ```bash
  poetry install
  ```

- Build the project (only generate the protos for now)

  ```bash
  poe build
  ```
