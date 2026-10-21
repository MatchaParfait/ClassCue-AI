# Contributing

## Development setup

Application Python modules live in `classcue/`; `api/` contains HTTP routes and
`scripts/` contains executable launch, preparation, and release tools. Use
package imports such as `from classcue.video_store import VideoStore` and keep
new application modules out of the repository root. See the directory map in
the development guide for entry points and installation-relative resources.

See [the development guide](docs/development.md) for local setup, startup
commands, API and model implementation details, and tests. Maintainers should
also read [the release and announcement guide](docs/releases.md).

Use Windows with Python 3.12 and Node.js 24 for the same environment as CI.
Install `uv`, then run:

```powershell
uv sync --locked --group dev
python scripts/check_repository_hygiene.py
Push-Location frontend
npm ci
Pop-Location
uv run --locked --group dev python -m pytest tests -q
Push-Location frontend
npm test -- --maxWorkers=2 --minWorkers=1
npm run build
Pop-Location
```

Runtime dependencies are declared in `pyproject.toml`. When they change, run
`uv lock` and commit the updated `uv.lock`; the one-click launcher uses the lock
file and an isolated `.runtime` environment. Do not commit `.runtime`.

## Pull requests

- Keep changes focused and include tests for changed persistence or pipeline behavior.
- Do not commit `data/`, `projects/`, `.env`, extracted audio, videos, models, or API keys.
- Use anonymized media and subtitles in issue reports, tests, screenshots, and documentation.
- Keep generated images, recovered assets, ASR run outputs, logs, and task caches local.
  Only copy reviewed, required product assets into their intended source directories.
- Stage explicit paths or use `git add -p`, then review `git diff --cached` and
  `git diff --cached --check` before committing. Do not stage the whole workspace
  without reviewing its contents.
- Run the checks above before opening a pull request. The hygiene check inspects
  the Git index, so rerun it after staging new or updated files.
- Keep cleanup, CI changes, and unrelated product changes in separate commits.
- Internal agent instructions and standalone experiment workflows stay private.
  Required shared scene modules under `experiments/subtitle-cyberspace/src/`
  remain application source.
- Add new runtime files to `release_manifest.toml`. Public tests, documentation,
  and build helpers do not automatically belong in the portable application.

See [repository maintenance](docs/repository-maintenance.md) for public/private
backup boundaries and the handling of previously published sensitive material.

## Optional browser regressions

Install Playwright in `frontend` with `npm install --no-save --package-lock=false playwright`,
then run the browser scripts in `tests/` against isolated fixture services.
Alternatively set `CLASSROOM_PLAYWRIGHT_MODULE` to an installed Playwright module.
Do not use real videos, credentials, or paid services for regression fixtures.

## Security and privacy

Report exposed credentials privately to the repository maintainers. Do not post
real DeepSeek keys or classroom recordings in public issues.
