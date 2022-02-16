# Temporal Python SDK

**UNDER DEVELOPMENT**

The Python SDK is under development. There are no compatibility guarantees nor proper documentation pages at this time.

### Local development environment

- Install the system dependencies:

  - Python >=3.7
  - [pipx](https://github.com/pypa/pipx#install-pipx) (only needed for installing the two dependencies below)
  - [poetry](https://github.com/python-poetry/poetry) `pipx install poetry`
  - [poe](https://github.com/nat-n/poethepoet) `pipx install poethepoet`

- Use a local virtual env environment (helps IDEs and Windows):

  ```bash
  poetry config virtualenvs.in-project true
  ```

- Install the package dependencies:

  ```bash
  poetry install
  ```

- Build the project (requires Rust):

  ```bash
  poe build
  ```

- Run the tests (requires Go):

  ```bash
  poe test
  ```