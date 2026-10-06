# Checklist

The implementer goes through it in self-review, the reviewer in round 1;
each item gets `ok` / `finding` / `n/a` — an empty line is a blocking
finding. Delete items that do not apply to the project; add items from
repeated findings.

## Acceptance criteria and tests

- [ ] Every acceptance criterion verifiable offline has a test that
  **fails** when the feature is disabled (mutation: comment out a line, the
  test must go red).
- [ ] Every conditional branch in a shaper (`if`, `in (…)`, type guard) has a
  canned payload that takes it and an assertion that fails when the branch is
  forced or removed.
- [ ] The test measures behaviour or parsed structure, not file text —
  `"x" in source` is satisfied by a comment too.
- [ ] Tests use only the fake Garmin client and canned payloads; they do not
  depend on network, global config, HOME, time zone or file order.
- [ ] New MCP tools are added to `EXPECTED_TOOLS` (and `WRITE_TOOLS` when they
  write) in the service's `tests/test_http.py` (`garmin-mcp/` or `coach-mcp/`), and the `tools/list` payload stays
  under the size limit asserted there (40 000 chars).
- [ ] `FakeGarmin.responses` in `garmin-mcp/tests/conftest.py` has a key for
  every new `get_*` call.
- [ ] Nothing was silently weakened: a deleted test has a "new home" line in
  the spec/PR.
- [ ] Every alert or notification is stated as a *state* (edge-triggered:
  new when it newly holds) or an *event* (about a different object each day,
  de-duplicated by the day or the object), and a test takes the consecutive-day
  case. A change to a per-day guard keeps the pre-fix case as a test.
- [ ] Time: fixed dates and relative dates in one test file are a calendar
  bomb — freeze the whole file or compute everything from `now`.
- [ ] The four-command check (`docker build --target test garmin-mcp`,
  `docker build --target test gateway`, `docker build --target test coach-mcp`,
  `scripts/e2e-test.sh`) was run and is green.

## Failure

- [ ] An error ends with a non-zero exit code or an exception, not a silent
  no-op; tool sections degrade fail-soft (`"error"` / `"available": false`).
- [ ] Zero, an empty string and a missing config value are validated at
  start-up, not deep inside a handler.
- [ ] Input of unbounded size is not read into memory whole.
- [ ] Clean-up is in `finally` / a cleanup hook, not on a single failure
  branch.

## Configuration and secrets

- [ ] A new config value has its default in one place and is documented
  (`.env.example`, README).
- [ ] No secrets, personal paths, hostnames, account ids or deployment
  identity in versioned code or in any commit (`git log -p`); `.env`,
  `secrets/` and `data/` are not in the diff.
- [ ] Logs and audit never contain tool arguments, results or health values,
  including what `garminconnect` itself logs for the new calls (check its
  source; its logger runs at WARNING). For any other library whose logs are
  silenced, the real `getLogger(...)` names were read in the image and each
  one is set and tested; a logger name is not always under the package name.
- [ ] A new dependency has a reason in the spec and is hash-locked through
  `scripts/lock-deps.sh` (no hand-edited `requirements*.lock`).

## Data

- [ ] Irreversible operations (deleting records or data) have a
  confirmation or a visible warning, and the README describes the way back.
- [ ] Writes to the athlete's Garmin/Hevy data are explicit write tools
  (annotated as not read-only) and need athlete confirmation.

## Documentation

- [ ] Docs and docstrings describe the new behaviour; grep for the old
  wording is empty. A tool-list change updates `garmin-mcp/README.md`,
  `README.md`, `docs/coach-instructions.md` and `INSTRUCTIONS` in `server.py`;
  `coach-mcp/README.md` and `INSTRUCTIONS` in
  `coach-mcp/src/coach_mcp/server.py` for coach tools.
- [ ] Czech docs stay Czech; code, comments and commits are English.
- [ ] Links in docs lead to existing files.
- [ ] Operator recipes added or changed in docs (backup/restore, recovery)
  were run on scratch volumes, fresh and existing, and the result is
  recorded.
