# Publish checklist for agentfin (plain words)

Nothing here has been run for you. Nothing was uploaded, tagged or pushed. The upload
step is yours: you are signed in to PyPI, this checklist never does it.

**Version to release: 0.4.0.** PyPI currently has 0.2.0 (re-checked 2026-11-08), so
0.4.0 is the next release. It changes behaviour compared with 0.2.0 (bad token counts
are refused instead of changed to 0, FOCUS column names changed, read commands no
longer create an empty ledger, an ambiguous invoice number like `1,5` is refused),
which is fine for a 0.x version but say so in the release notes.

People who already use 0.2.0: their ledger and config files work unchanged (a real
0.2.0 ledger is a test fixture). Their old `pricing.yaml` lacks newer models; `agentfin
doctor` tells them, and `agentfin pricing-template` shows the new example.

## Check first

1. `git log -1` shows a "Round 5:" commit on branch `expert-2`.
2. In the repo: `python -m pytest -q` (expect 307 passed) and `ruff check .`
   (expect "All checks passed").
3. Author name: the package says **Calas Automations Inc.** (no email on purpose). If
   you want an email shown on the PyPI page, add it yourself in `pyproject.toml`.
4. Project links: none are in the package page because this session did not know
   whether the GitHub repository is public. If it is public, add to `pyproject.toml`:
   `[project.urls]` then `Homepage = "https://github.com/calasdan-sketch/agentfin"`.
   If it is private, leave it out (a link nobody can open looks broken).
5. Optional but recommended: push the branch to GitHub once. The file
   `.github/workflows/test.yml` then runs the tests on Windows, macOS and Python
   3.10 for the first time. Read any red results before announcing support.

## Build and test the files (safe, local)

```
python -m pip install build twine
python -m build
python -m twine check dist/*
python -m venv /tmp/try && /tmp/try/bin/pip install dist/*.whl
/tmp/try/bin/agentfin --version      # should say: agentfin, version 0.4.0 (Calas Automations Inc.)
/tmp/try/bin/agentfin demo           # should show acme-api $0.61 and docs-site $0.39
unzip -p dist/*.whl 'agentfin-0.4.0.dist-info/METADATA' | grep -E '^(Author|Maintainer)'
                                     # both lines should say: Calas Automations Inc.
```
(On Windows use `\try\Scripts\agentfin`.)

## Upload (only when you decide)

```
python -m twine upload dist/*        # needs your PyPI login/token
git tag v0.4.0 && git push --tags    # after you merge the branch
```
Then in a clean place: `pip install agentfin==0.4.0` and run `agentfin demo`.
After release, delete the "Heads-up: PyPI has 0.2.0" paragraph in the README Install section.

## Optional extra check (takes about 15 minutes)

`python benchmarks/mutation_check.py` deliberately breaks the code 37 ways and expects
the tests to catch every one. It must end with "0 survived".
