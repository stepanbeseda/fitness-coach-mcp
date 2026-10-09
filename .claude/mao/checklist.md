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
- [ ] Every optional argument of a new tool has a call through the real
  entry point (HTTP) with a value that differs from the default, and
  dropping the argument in the wiring turns a test red. The tests accept
  every limit's maximum and reject maximum + 1; two checks that can both
  apply have a case where the order decides.
- [ ] A test that builds a database of version N uses `MIGRATIONS[:N]` for
  the build and the reopen; the latest version is derived, not hand-pinned.
- [ ] Parallel or rebased work: docker builds and e2e run under `flock
  /tmp/fcm-docker.lock` with PR-private image tags, and after a rebase the
  test stage is forced (`--no-cache-filter test`).

## Failure

- [ ] An error ends with a non-zero exit code or an exception, not a silent
  no-op; tool sections degrade fail-soft (`"error"` / `"available": false`).
- [ ] Zero, an empty string and a missing config value are validated at
  start-up, not deep inside a handler.
- [ ] Input of unbounded size is not read into memory whole.
- [ ] Clean-up is in `finally` / a cleanup hook, not on a single failure
  branch.
- [ ] Retry/give-up logic: run the backend-outage scenario (every call errors
  for N runs, then recovers) and confirm no item is given up permanently and
  nothing is charged for the outage.
- [ ] A date or timestamp that comes from a backend is bounded by year when
  parsed or parsed under `except (ValueError, OverflowError)`; a test feeds
  9999-12-31 and 0001-01-01 through the real path.
- [ ] A statement in the spec or code about what a library or backend
  returns (error text, status, output schema, nullability, a shared error
  code) was checked against the real image; HTTP tests of tool errors assert
  "contains" because the MCP SDK prefixes the text. A new secret whose
  absence degrades a service shows in `/healthz` and
  `scripts/healthcheck.sh`, or the gap is accepted in writing.

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
- [ ] A test that a library logger stays quiet resets it with
  `setLevel(logging.NOTSET)` and asserts its own `.level`; `getEffectiveLevel()`
  passes vacuously under pytest. Removing the logger from the quieting list
  turns it red.
- [ ] A new dependency has a reason in the spec and is hash-locked through
  `scripts/lock-deps.sh` (no hand-edited `requirements*.lock`).

## Data

- [ ] Irreversible operations (deleting records or data) have a
  confirmation or a visible warning, and the README describes the way back.
- [ ] Writes to the athlete's Garmin/Hevy data are explicit write tools
  (annotated as not read-only) and need athlete confirmation.
- [ ] A rotation or clean-up never makes the item written by this run a
  candidate and does not report success without it; `find ... -exec rm`
  meant for children has `-mindepth 1`. A settled/final flag takes the
  latest settle time of every source that feeds it.
- [ ] New scripts are committed executable (mode 100755 like the other
  `scripts/*`), and the spec says `chmod +x`.

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
- [ ] A recipe that appends to a file (`>> .env`) states the outcome of a
  second run (a duplicate line) and what the user does about it; no step
  that a worker runs opens `.env` or `secrets/*`.
- [ ] After a rebase onto, or integration with, a merged PR, `INSTRUCTIONS`,
  tool docstrings and the Czech docs are grepped for claims the other PR
  made false (a source list, "not stored yet", "Garmin only"), and every
  negative grep first shows its positive hit on the current tree.
- [ ] A deploy step states the order (pull, `setup-secrets.sh`, `up --build`
  of all affected services together) and the commands that must not run in
  between; none of them recreates a service from the old image under the new
  compose file.
