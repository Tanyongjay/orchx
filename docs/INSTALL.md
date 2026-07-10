# Install

OrchX v0.2.x is distributed via Git tags. There is **no
PyPI release yet** — publishing to PyPI requires a manual
one-time setup (PyPI 2FA + trusted publisher) that the
maintainer hasn't done yet.

Until then, the recommended install path is `pip install
git+https://...` from a pinned tag.

## Quick install (recommended)

```bash
# Pin to a specific tag for reproducibility.
pip install "git+https://github.com/Tanyongjay/orchx.git@v0.2.0"
```

This installs:
- The `orchx` CLI entry point (so you can run `orchx plan`,
  `orchx deploy` from anywhere).
- The `orchx.web` module (so you can start the dashboard
  with `python -m orchx.web.app`).
- All Python dependencies: typer, rich, pydantic, pyyaml,
  httpx, anyio, ruamel-yaml.

## Install with the dev or web extras

```bash
# Development install (pytest, ruff, mypy, freezegun, fastapi,
# uvicorn, aiosqlite, httpx — everything you need to run the
# test suite or work on the source).
pip install "git+https://github.com/Tanyongjay/orchx.git@v0.2.0#egg=orchx[dev]"

# Real-transport extras (pywinrm + asyncssh). Only needed
# if you're targeting real WinRM or SSH hosts.
pip install "git+https://github.com/Tanyongjay/orchx.git@v0.2.0#egg=orchx[real,web]"
```

## Clone and install (if you want the sample descriptors too)

```bash
git clone --branch v0.2.0 https://github.com/Tanyongjay/orchx.git
cd orchx
uv sync --extra dev          # if you have uv
# or:
pip install -e ".[dev]"      # editable install via PEP 517
```

This gives you:
- The `orchx` CLI on your `$PATH`.
- The sample descriptors under `descriptors/` so you can
  experiment without writing your own YAML.
- The docs under `docs/`.
- The test suite under `tests/`.

## Verify

```bash
orchx --help
orchx plan --help
orchx deploy --help
```

You should see the three subcommands and the new
`--verbose` / `--json` flags on `deploy`.

## Run the web control plane

```bash
# If installed via pip:
python -m orchx.web.app
# If installed via uv sync:
uv run python -m orchx.web.app
```

The dashboard listens on `http://127.0.0.1:8000/` by default.
OpenAPI is at `http://127.0.0.1:8000/api/docs`.

## Migrating to PyPI later

When the maintainer enables PyPI, the install line will
become:

```bash
pip install orchx
```

The same extras will still apply:

```bash
pip install "orchx[dev]"
pip install "orchx[real,web]"
```

No code changes will be required. The change is purely in
the distribution mechanism.

## Troubleshooting

- **`orchx: command not found`** — your `pip install` did
  not put the script on `$PATH`. Try `python -m orchx.cli.app`
  instead, or use a virtualenv.
- **`ImportError: No module named orchx`** — make sure the
  `pip` you used matches the `python` you run. In a venv
  they're the same; in a global install you may need
  `python3 -m pip install ...`.
- **No descriptors in the dashboard dropdown** — make sure
  your `descriptors/` directory is in the current working
  directory when you start `python -m orchx.web.app`, or
  pass an absolute descriptor path when calling the CLI.