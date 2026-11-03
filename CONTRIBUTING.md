# Contributing

## Development

Development and CI use Python 3.14 and Home Assistant 2026.10.4. Install the
pinned test tools with `python -m pip install -r requirements-test.txt`.
Run the controller, lifecycle, configuration and release-asset tests from the repository root:

```bash
python -m pytest tests
```

Run static checks before a release:

```bash
ruff check .
ruff format --check .
python -m compileall custom_components tests
```
