# Publish checklist for agentfin (plain words)

Nothing here has been run for you. Nothing was uploaded, tagged or pushed.

**Version to release: 0.4.0.** PyPI currently has 0.2.0 (checked 2026-11-07), so 0.4.0 is
the next release. It changes behaviour compared with 0.2.0 (bad token counts are refused
instead of changed to 0, FOCUS column names changed, read commands no longer create an
empty ledger), which is fine for a 0.x version but say so in the release notes.

## Check first

1. `git log -1` shows the round 4 commit on branch `expert-2`.
2. In the repo: `python -m pytest -q` (expect 237 passed) and `ruff check .` (expect "All checks passed").
3. Put your repository link in `pyproject.toml` under `[project.urls]` (this was left out
   because the address was not known). Example:
   `Homepage = "https://github.com/<you>/agentfin"`. Do the same on the project page afterwards.
4. Read `LICENSE`: it says "Copyright (c) 2026 Calas Automations Inc." Change it if you want a different owner name.
5. Optional but recommended: run the tests once on Windows (and Python 3.10) before announcing it as supported.

## Build and test the files (safe, local)

```
python -m pip install build twine
python -m build
python -m twine check dist/*
python -m venv /tmp/try && /tmp/try/bin/pip install dist/*.whl
/tmp/try/bin/agentfin --version      # should say 0.4.0
/tmp/try/bin/agentfin demo           # should show acme-api $0.61 and docs-site $0.39
```
(On Windows use `\\try\\Scripts\\agentfin`.)

## Upload (only when you decide)

```
python -m twine upload dist/*        # needs your PyPI login/token
git tag v0.4.0 && git push --tags    # after you merge the branch
```
Then in a clean place: `pip install agentfin==0.4.0` and run `agentfin demo`.
After release, delete the "Heads-up: PyPI has 0.2.0" paragraph in the README Install section.
