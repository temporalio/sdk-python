# Temporal Python SDK

### Local development enviroment

- Install the system dependencies

  - Python >=3.7
  - [pipx](https://github.com/pypa/pipx#install-pipx)
  - [`poetry`](https://github.com/python-poetry/poetry) `pipx install poetry`
  - [`poe`](https://github.com/nat-n/poethepoet) `pipx install poethepoet`

- Use a local virtual env environment (helps IDEs and Windows):

  ```bash
  poetry config virtualenvs.in-project true
  ```

- Install the package dependencies

  ```bash
  poetry install
  ```

- Build the project

  ```bash
  poe build
  ```
