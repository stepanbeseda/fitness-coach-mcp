# Lessons from MAO runs

Memory shared across PRs and worktrees of this project. Agent `memory:` is
stored per worktree, which is why this is a file in the repo. The
orchestrator adds findings whose shape repeats at the end of a run. All
agents read it before working; general lessons are in
`~/.claude/skills/mao/references/lessons-general.md`.

## Error shapes that repeated

- **A guard branch in a shaper that no test fails without.** In
  `shape_personal_records` the `activityType` check (`running = atype in
  (None, "running")`) survived the mutation `running = True` with the whole
  suite green. Every `if` / `in (…)` that routes a payload to a different
  output needs a canned payload that takes the other branch and an exact
  assertion on the result; mutate each branch once in self-review. (PR 01,
  review 1, N1)

- **A tool-argument test whose values equal the defaults.** The prescribed HTTP test of `get_correlations` used
  `lag_days: 0` and a date range that coincides with the defaults for the seeded data, so a `server.py` that
  dropped `lag_days`, `start_date` and `end_date` kept the suite green (two mutants, review round 1, blocking).
  Same shape as the guard-branch entry above, one level up: the wiring from the tool signature to the function.
  Every optional argument of a tool gets one call through the real entry point with a value that changes the
  result, and the mutation that drops the argument turns it red. (PR 16, review 1, B1; spec spells the weak
  test, partial spec error)
- **Limits and check order without a failing test.** Repeats the guard-branch shape: validation branches with no
  named case (PR 06 spec check N4), the `length(evidence) <= 2000` branch of a CHECK (PR 13 spec check N5),
  `lag_days=3` and exactly six metrics never accepted, so `MAX_LAG_DAYS = 2` survives, and two checks whose
  swapped order survives because no case triggers both (PR 16 spec check N2). For every limit the tests accept
  the maximum and reject maximum + 1; for two checks that can both apply there is one case where both do and the
  order decides the result.
- **`OverflowError` from datetime arithmetic on an external date.** `astimezone` and `date + timedelta` on a
  value at the edge of the calendar raise `OverflowError`, which `except ValueError` does not catch: a 200
  payload dated 9999-12-31 escaped as an unexpected tool error (PR 06, review 1, N1) and one Hevy event with
  `9999-12-31T23:00:00-05:00` would crash the strength part every hour for good (PR 09, review 1, N2). Dates and
  timestamps that come from a backend are either bounded by year when parsed (PR 06: 1900-8999) or parsed under
  `except (ValueError, OverflowError)`, and a test feeds 9999-12-31 and 0001-01-01 through the real path.
- **Model-facing text and docs falsified by another PR.** Three texts still said "suggestions come from the
  Garmin activities" after PR 09 added Hevy evidence (`INSTRUCTIONS`, the `get_plan` docstring, the
  `reconcile.suggest` docstring; PR 09 review 2, N1, a spec error because spec 12.4 said "unchanged"); PR 16's
  docstring "Hevy history is not stored" went stale after PR 09 (PR 16 impl-3); PR 09's own spec check found two
  README sentences ("Sám volá …", "jeden řádek na běh") and a "silové tréninky z Hevy se zatím nepárují" phrase
  that its change made false (spec check N4, N6). When a PR is rebased onto, or integrates with, a merged PR,
  grep `INSTRUCTIONS`, tool docstrings and the Czech docs for claims the other PR made false, not only for the
  PR's own old wording. A negative grep first shows its positive hit on the current tree: a phrase that wraps
  over two lines made a "no output" check vacuous (PR 13 spec check N1).
- **A test builds a database of version N with the full migration list.** PR 13's `test_downgrade_4_sql_...`
  built a "v4 store" with all of `MIGRATIONS` and reopened with the full tuple expecting 4; once PR 09 added
  entry 5 it would have built version 5 and failed on re-running migration 5 (PR 09 spec check N5). A test of
  version N uses `MIGRATIONS[:N]` for both the build and the reopen, and the latest version is derived
  (`LATEST_VERSION`), never hand-pinned in more than one place (PR 15 rebase).

## Spec error shapes (designer)

- **A third-party library logs identifiers the spec never mentioned.**
  `garminconnect.get_gear_stats` logs `Gear stats not found for UUID <uuid>`
  at WARNING on a 404, and `__main__.py` keeps that logger at WARNING, so
  athlete identifiers reach the container log although plan section 1 says
  logs carry only tool name, outcome and duration. For every new library
  call the spec states what the library itself logs (read the source in the
  image) and either quiets that logger/record or accepts it explicitly.
  (PR 01, review 1, N2)
- **A spec anchor that does not sit on one line of the target file.** Cited
  line numbers were off (`~15 vybranými` is on line 4, not 3; the README
  bullet is on lines 7–9, not 5–7) and a quoted comment wraps across two
  lines, so a literal search/replace does not match. Quote only text that is
  on a single line, give the line from `grep -n` run on the current tree,
  and say "re-wrap" where a comment must change. (PR 01, spec check W1–W3)

- **A script branch that only runs against the live project cannot be
  exercised by the check.** Token creation plus service recreate in
  `scripts/setup-secrets.sh` runs only against the live compose project, so
  `bash -n` alone proves nothing. The spec says how the branch gets evidence:
  an offline stub harness on the extracted function, or a named live step.
  (PR 03, spec check finding 4; review 1, N9: an empty token file counted as
  present)

- **An operator recipe prescribed verbatim in the spec was never run.**
  Backup, restore and recovery steps are executed on scratch
  volumes/containers before the spec prescribes them, including the
  fresh-volume case and the case where a "safety backup" could overwrite the
  archive being restored. In PR 03 the README restore recipe failed on a
  fresh volume and cost a review round and a design attempt. (PR 03, review
  2, B1)

- **An edge-triggered de-duplication rule applied to an event-type condition.** "Alert only when the condition
  newly holds" suits a lasting state (HRV low, auth broken). A condition that is about a different object each
  day (yesterday's missed workout) must be de-duplicated by the event itself (the day / the object), or the second
  event is silently swallowed. For every alert or notification, the spec says whether it is a *state* or an
  *event* and what its de-duplication key is. (PR 04, review 1, N1)
- **A third-party logger name is not under the package name.** The spec silenced `mcp.client` to keep the MCP
  SDK quiet, but the client session logs through `logging.getLogger("client")` (`mcp/client/session.py:71`),
  which stays at the root level and can carry payload text in `exc_info`. For every library whose logs the spec
  quiets, list the real `getLogger(...)` names with `grep -rn getLogger` in the installed package inside the
  image, and set and test every one of them. (PR 04, spec check F4)
- **A fix that changes a per-day guard drops a case the old guard handled.** The A5 fix set "first look
  of the day" before the store read, so a store error on a late first look (service started after the cutoff)
  turned the late start into a late send. A spec fix that re-keys or reorders a per-day / first-look guard lists
  every case the old code handled (including the failure paths) and keeps each one as a test next to the new
  case. (PR 04, review 2, N10)

- **A prescribed code block contains the word the acceptance grep forbids.** The spec's comment above `_CIRCLE`
  said "the circles ntfy showed", so the criterion "`grep -rni ntfy` finds only the rollback note" could not pass
  after pasting it. When a criterion is a grep, run that exact grep over the spec's own code blocks and prose
  (`grep -n -i <word> <spec>`) and reword every hit that is destined for the repo. (PR 08, spec check F1)
- **A brief where one criterion forbids what another requires.** Done-when item 4 ("no ntfy anywhere") clashed
  with item 7 (the rollback note must name ntfy) and was caught only in the spec phase. Before the brief is
  confirmed, read every "none / no / only" criterion against every criterion that creates the thing, and write
  the exception into the forbidding one ("finds only the README rollback note of item 7"). (PR 08, orchestrator
  decision after the brief)
- **A "logger stays quiet" test that asserts the effective level passes vacuously under pytest.** Pytest installs
  root handlers, so `basicConfig(level="DEBUG")` is a no-op, the root stays at WARNING and every unconfigured
  logger already has effective level WARNING: the prescribed `getEffectiveLevel() >= WARNING` stayed green with
  `"httpx2"` removed from `configure_logging`. Reset the logger with `setLevel(logging.NOTSET)` first and assert
  its own `.level`, and run the mutation that removes the logger from the list. (PR 08, spec check F2; PR 04
  F4 is about finding the logger names, this is about the assertion)
- **A worker step in a live procedure reads `.env` or `secrets/*`.** `grep -c '^X=' .env` prints only a count but
  still opens the file, and `project.md` forbids it; the same holds for `stat`/`ls` on `secrets/*` unless the
  orchestrator's pre-approval is stated in the step. Live steps put such commands in a "user command" list
  (the user pastes the count) and name the approval for metadata-only reads. (PR 08, spec check F4)
- **An append recipe run twice duplicates its line.** `printf '\nX=%s\n' "$V" >> .env` followed by
  `grep -c '^X='` prints `2` the second time, and the spec said nothing about it. Every live recipe that appends
  states what a second run leaves behind and what the user does then (remove the duplicate line). (PR 08,
  review 1, N7)
- **A per-item failure counter charged for a backend-wide outage.** garmin-mcp answers during a Garmin Connect /
  internet outage with every section as `{"error": …}` and `/healthz` still says `garmin_auth: ok`; the spec charged
  each such error to the day, so three hourly runs gave up every pending day permanently. For every retry/give-up
  rule the spec must separate "this item is bad" from "the service is down" with an explicit test (probe of a
  known-good item, or no permanent give-up), and list the outage scenario among the tests. (PR 05, review 1, B1)
- **An addendum that changes a guard without grepping the existing tests of that guard.** Spec check 2 found an
  existing test asserting the opposite of the new `needs_fetch` rule and two tests whose fakes would make the new
  probe fail. For every function an addendum changes, the designer greps the test files for its name and lists each
  hit as kept / changed / removed. (PR 05, spec check 2, G1–G3)

- **A statement about what a third-party service returns, written from memory.** The MCP SDK prefixes every
  `ToolError` text over HTTP with `Error executing tool <name>:`, so the prescribed exact-text assertion could
  never pass over HTTP (PR 06 spec check B1); `hevy-mcp` answers a 429 as a structured `HEVY_RETRY_EXHAUSTED`
  status, not a "Rate limited" text, so the prescribed classification never fired (PR 09 spec check B1); the
  real output schema of `get-workout-events` requires `duration` and rejects nulls, so the spec's sample
  payloads would have failed the e2e step on correct code (PR 09 N1); `SQLITE_ERROR` is also the code of `no
  such table` (PR 15 N2). Before the spec states an error text, status, schema or nullability, the designer
  reads or runs it in the image (`list_tools()`, the library source, a stub answering the status). HTTP tests of
  tool errors assert that the response contains the text.
- **A rotation or clean-up rule that assumes the new item is the newest.** Backup rotation by name sort deleted
  the archive just written when `BACKUP_DIR` held `keep` or more later-named archives (clock corrected, copies
  from another host), printed a success line with a failed `du` and exited 0, so every later cron run silently
  lost its own backup (PR 11, review 1, B1, a spec error: the spec prescribed the plain name sort). A retention
  rule never makes the item written by this run a candidate, warns when it meets a newer-named one, and refuses
  to report success without the file. The fix itself added `find "$dir" -maxdepth 1 -name '.backup-tmp.*' -exec
  rm -rf`, which also matches the start directory; a `find ... -exec rm` meant for children of X needs
  `-mindepth 1` (PR 11, review 2, N12).
- **A new parameter whose name is already a local of the function.** The spec added `yesterday=()` to
  `alert_conditions`, which already rebinds a local `yesterday` to the Garmin calendar entries before the place
  where the new condition reads it, so the new condition would read the wrong object (PR 13 spec check B1). For
  every new parameter the designer greps the function body for the name.
- **A finality flag that ignores a source added later.** `final` of a day's suggestion came from the Garmin
  re-fetch rule (D+3); once PR 09 fed Hevy workouts into the same matching, a strength `no_activity` could be
  `final` while Hevy was still re-read (D+4 at the earliest, indefinitely during an outage) and later turn into
  `matched` (PR 09, review 2, N2). When a settled/final flag is computed per day, the spec lists every source
  that feeds the decision and takes the latest settle time of all; a per-sport answer is a design change, not a
  patch.
- **Operator recipes with commands that were not run, second round.** Same shape as the PR 03 entry: a
  here-document replaced the pipe as stdin of `python3 -` so the script read `b''` (PR 11 spec check N1); a step
  used `"$BACKUP_DIR"` in a user shell where it is only a `.env` entry (N5a); the example archive name used host
  time although names are UTC (N5b); the restore recipes call image tags that hold the old code until the
  post-merge deploy, so they end with the usage line (N8); the targeted gateway test command used `python`, but
  pytest lives in `/venv/bin/python` in the test stage (B1), and PR 09's targeted command needed an image that
  did not exist yet (N3). The spec check runs every command of the spec block once, in the image, from the shell
  the user has.
- **A resource bound estimated instead of measured.** The spec budgeted "2 x 32 MiB" for the backup; the
  measured peak was about 3x (CPython `serialize()` alone is 3.15x), so the test used 3.5x (PR 11 spec check N3,
  review 1 item 8). The Hevy e2e step was said to fit the "~50 s" e2e but takes 23 s more against an unreachable
  API (PR 09 spec check N8). Memory, time and size bounds in a spec carry a number measured in the image.
- **`PRAGMA foreign_keys = ON` inside an open transaction is a no-op.** A foreign-key test that ran the pragma
  after an earlier failed INSERT accepted session id 999 (PR 13 spec check N4). The pragma comes first on a
  fresh connection.
- **Line anchors and 120-character lines, again.** Repeats the PR 01 anchor entry: comment and sentence anchors
  that wrap over two lines or sit a few lines off (PR 06 N3, PR 13 N2 and N3, PR 16 N5), a prescribed
  `.env.example` comment of 120 characters against the 118 limit (PR 11 N2), a prescribed CHECK line over 120
  (PR 13). The designer pastes the anchor from `grep -n` on the tree and measures the prescribed text with the
  Python one-liner in the operational lessons.

## Operational lessons

- **`docker compose build` with stdout redirected needs an explicit progress mode.** A newer
  Compose picks the tty renderer when stderr is a terminal and then fails on the redirected
  stdout with `failed to get console: provided file is not a console`. `scripts/e2e-test.sh`
  builds with `--progress=plain` into a log file since PR 02; use the same in any new script
  that silences a build. (PR 02, local check)
- **A MAO run does its code work in a separate git worktree.** The live stack
  runs from the main checkout (`gateway/config.toml` is bind-mounted, the
  compose project name is pinned), so the worktree is created in preflight,
  run files stay in the main checkout's `.claude/mao-log/`, and the live tree
  gets the PR's code only in an approved deploy step. The command rules are
  in `project.md` ("Commands"). (PR 03)
- **Measure line lengths with Python, not `awk`.** The host `awk` is mawk, which counts bytes, so a
  length check on Czech text reports 18 lines instead of 4. Specs prescribe
  `python3 -c "import sys; [print(i, l[:40]) for i, l in enumerate(open(sys.argv[1], encoding='utf-8').read().split('\n'), 1) if len(l) > 118]" <file>`
  for character counts. (PR 04, spec check F3)
- **`scripts/e2e-test.sh` retags the image the live stack would be recreated from.** Run in a worktree, it
  retags the shared `fitness-coach-mcp/coach-mcp:0.1.0` to the PR build, so a plain `docker compose up -d` in
  the live checkout would recreate the service from the new image under the old compose file and migrate the
  live database without a backup. Before any deploy step compare the running container's image ID with the
  tag (`docker inspect -f '{{.Image}}' <container>` against `docker image inspect -f '{{.Id}}' <tag>`), say
  so in the live-step warning, and run no compose command that creates `coach-mcp` before the backup. (PR 04,
  review 1, N6) Since 2026-10-09 the e2e test builds `fcm-e2e/*:test` tags and no longer retags the live ones;
  the image-ID check before a deploy stays, because a plain `docker compose build` in a worktree still does.
- **A recreate of one service under a changed compose file.** Same shape as the PR 04 retag entry.
  `setup-secrets.sh backend-tokens` and `telegram-token` recreate `coach-mcp` from the existing image without
  `--build`; run after `git pull` but before the deploy of PR 12 they start the old image under the new compose
  file, which no longer mounts `garmin_backend_token`, and briefing and history report `credentials_missing` (PR
  12, review 1, N2). A deploy step for a PR that changes mounts or secrets states the order (pull,
  `setup-secrets.sh`, `up --build` of all affected services together), names the commands that must not run in
  between, and says what the old container does if `up` runs before the secret exists (it fails on the bind
  source and leaves the old containers running; probed in PR 12). A new secret whose absence degrades a service
  is visible in `/healthz` and `scripts/healthcheck.sh`, or the gap is accepted in writing (PR 12 N1).

## Parallel runs (several PRs at once, run of 2026-10-09)

Seven PRs ran in parallel worktrees (06, 09, 11, 12, 13, 15, 16) on a 2-CPU host and were merged one
by one. The rules below came from `PARALLEL.md` of that run and held without a lost build or a wrong
image.

- **Docker builds and e2e runs go through one lock.** `flock /tmp/fcm-docker.lock docker build ...` and `flock
  /tmp/fcm-docker.lock scripts/e2e-test.sh`: two CPUs and 11 GB do not take two builds, and the e2e project
  `fcm-e2e` is a single name. Waiting for the lock is normal; nobody kills another PR's build.
- **Targeted test images carry the PR number.** `fcm-coach-test-pr<NN>`, `fcm-garmin-test-pr<NN>`,
  `fcm-gateway-test-pr<NN>` (reviewers `-review-pr<NN>`), never the bare `fcm-coach-test`. A build and the
  `docker run` that uses it happen under one lock hold, or the image is rebuilt right before the run. The
  gateway test image has pytest in `/venv/bin/python`, not `python`.
- **The `fcm-e2e/*:test` tags belong to whoever ran e2e last.** They are the same for every PR, so a scratch run
  that "uses the images the e2e built" may hold another branch's code without `backup`/`restore` and record
  wrong evidence (PR 11 spec check B2). The `fcm-e2e` images are used only inside one's own locked
  `scripts/e2e-test.sh`; any other `docker run` builds a PR-private tag (`fcm-<svc>-<purpose>-pr<NN>`).
- **A cached test stage proves nothing after a rebase.** `docker build --target test` reported the test layer
  CACHED for an unchanged tree (PR 09 impl-1). After a rebase or a conflict resolution the check is run with
  `--no-cache-filter test` so the suite really runs; reviewers do the same.
- **Migration numbers are renumbered on rebase, mechanically.** The spec of a PR with a migration is written for
  the next number after `main`; the PR that merges later renumbers (PR 13 stayed 4, PR 09 became 5, PR 15 became
  6, none of the three specs was wrong). That works when the migration is one self-contained block, tests derive
  the number (`LATEST_VERSION`, `MIGRATIONS[:N]`, see the entry above), and the downgrade recipes are re-run on
  scratch volumes after the renumbering and chained (5 to 4 before 4 to 3). Cross-PR integration the earlier PR
  cannot test (Hevy evidence in PR 13's matching) is written into the later spec as a numbered "after the
  rebase" section and applied in the rebase round, with its own review.
- **`git rebase --onto` after a squash merge of the base branch.** When PR A, the base of PR B's local branch,
  is squash-merged, a plain `git rebase origin/main` replays A's original commits and conflicts with their
  squashed copy. Use `git rebase --onto origin/main <old A tip>` (journals of PR 15, 16 and 06: clean, or the
  same tree). If `git diff <checked tip> HEAD` is empty the tree is the one that was checked and the check need
  not run again (PR 16, PR 06).
- **Shared files are edited additively; conflicts keep both sides.** `server.py` (registration, `INSTRUCTIONS`),
  `tests/test_http.py` (`EXPECTED_TOOLS`), the README tool tables, `docs/coach-instructions.md`, `.env.example`,
  `docker-compose.yml`, `scripts/*` get appended lines. The tool count asserted in tests and e2e is fixed on
  rebase. Review of a rebased tree reads the conflict resolutions for behaviour of the merged PRs that got
  dropped (PR 06 review 2 and PR 09 review 2 found none) and the request ids of `scripts/e2e_flow.py` (PR 09 and
  PR 12 both used id 11 on different endpoints, harmless).

## Batch of 2026-10-10 (PR 18 and PR 19)

Two PRs in separate worktrees, merged in plan order (#26, #27), the second rebased after the first.

- **Run the expected values of a spec; do not read them.** The one blocking spec-check finding of the batch was a
  test whose prescribed result contradicted the code: PR 18 T19 expected `unreachable` for `auth = "missing"`, the
  code returns `auth` (`unreachable` is `auth=None`). Reading `history.py` and the existing test would have shown
  it, but only applying the spec to a scratch copy and running its tests proved it. The PR 19 spec check, which
  also ran the prescribed tests and mutations, found a mutant that survived because the prescribed tests did not pin
  the indentation of three statements (they moved into the HR-zones block and the suite stayed green). The spec check
  applies the spec literally to a throwaway copy and runs every prescribed test and mutation. (PR 18 spec check B1;
  PR 19 spec check N2)
- **Mutation tables in specs lack predicate mutants.** Both reviewers found survivors that the specs' tables did not
  list: PR 18 the charge rule `rest <= run.sections_ok` ("any" against "every"), repair answers counted as
  evidence, and `_confirm` removed from the repair (round 1, fixed in round 2); PR 19 four gaps on correct code
  (max weight against max of rising weights, a lower-case `rest`, a probability tie, a nested key type guard). The
  table lists a predicate mutant for every comparison, quantifier (`any`, `all`, `sum`), case fold, tie rule and type
  guard, with the case that kills it; the designer proposes the mutants, the reviewer adds its own. The new
  checklist item says the same. (PR 18 review 1, O1 and O2; PR 19 review 1, O1)
- **Shape-only capture from the live container settles assumptions before the implementation.** The owner ran two
  read-only scripts (`docker compose exec -T garmin-mcp python - < capture.py`) that print key names, types and list
  lengths and no values. They showed that the activity summary carries `activeSets` and `totalExerciseReps` (added as
  D12), that the strength set has `repetitionCount` and `weight` null and the weather of a strength activity is all
  null, and that the running tolerance is empty on the account for every range (a request bug ruled out by a second
  capture over 56 and 182 days and daily). The designer writes the capture command into the spec, the orchestrator
  asks the owner, the capture goes to the same designer as an addendum (not a new attempt), and the fixtures stay
  synthetic. Criterion 5 of PR 19 was then reworded in the PR body, not in the plan. (PR 19, D11, D12)
- **A cleanup glob deletes other agents' files.** The PR 18 reviewer removed `/tmp/fcm-check-pr18.*` with a glob; the
  directories are the logs of every `scripts/check.sh --tag pr18` run, including the implementer's, which were
  evidence at that moment. An agent removes only what it created, by the exact name it recorded when it created it
  (a log directory printed at the start of its own run); a glob is allowed only over files the agent itself created.
  (PR 18 review 2, Hygiene)

## Batch of 2026-10-11 (PR 14 and PR 20)

Two PRs in separate worktrees, merged in plan order (#29, #30), the second rebased after the first without conflicts.

- **A prescribed expected value must be run through the real helper.** The PR 14 spec check found a blocking error:
  T-P3 prescribed that the string `"1290"` is dropped, but `formatting.num` does `float(value)` inside `try` and
  keeps numeric strings, so the test could not pass. The designer runs every expected value of a test through the
  helper the code uses (not through its docstring or memory of it), and writes an explicit type guard when the
  behaviour must differ from the helper. Same family as the PR 18 lesson above. (PR 14 spec check B1)
- **Every case a criterion enumerates has a row in the test harness.** Criterion 2 of PR 20 named the unreadable
  token file; the harness had no row for it, so a mutant that dropped the problem lines from the cron mail survived,
  and review 1 returned CHANGES for it (the only blocking finding of the PR). The spec lists the cases of each
  criterion next to the rows that test them; a case without a row is a spec-check finding. The new checklist item
  says the same. (PR 20 review 1, B1)
- **Synthetic ids in tests are obviously fake.** The PR 20 spec gave the harness a random-looking 10-digit chat id.
  Neither the reviewer nor the orchestrator could tell it from a real one (the reviewer could not read `.env`), so
  the branch was squashed to one commit before the push to keep it out of the history. Use values like `123456789`
  or `000000000`; if one slips in, squash before the push, which is free until the branch is published. (PR 20
  review 2)
- **Post-approval non-blocking test gaps are cheap in one commit and a delta review.** PR 14 review 1 approved with
  three test gaps and wrong doc rows (O1, O2, O4); the implementer fixed them in one commit, ran the three mutants
  against the new tests, and a delta review of only that commit approved. No second full review, no new design
  attempt. (PR 14 impl 2, review 2)
- **`gh pr merge --delete-branch` fails locally while `main` is checked out elsewhere.** The live checkout holds
  `main`, so after the squash merge `gh` could not switch to it to delete the local branch and printed an error,
  but the merge itself had happened (check with `gh pr view <n> --json state,mergeCommit`). Delete the remote
  branch with `git push origin --delete <branch>` and the local branch and worktree by hand. Do not run
  the merge again.

## Batch of 2026-10-11 (PR 17)

One PR (#33), review in one round (APPROVED, 0 blocking, 8 non-blocking), no rebase.

- **A spec that changes a shared list does not list the existing tests that hold a literal copy.** The PR 17 spec
  put `get_device_status` first in the briefing tool list and the fake app; the spec check found three existing
  tests that went red and were not in section 6.3: two expected call dicts and a `wait_for(... == ["get_recovery_snapshot"])`
  that can never hold (10 s timeout). Same family as the addendum lesson above (grep the tests of a changed
  guard), one level up: a changed list, call order or fake. The designer greps the tests for the name and for the
  literal values, and lists each hit as kept / changed. New checklist item. (PR 17 spec check N2)
- **A parameter inserted in the middle of a signature breaks positional callers.** The spec put `missed_stale`
  "after `missed_sessions`" in `render()`, whose caller passed `language` as the 11th positional argument; applied
  literally it is a `TypeError`. The spec says "last, or by keyword" and names the call site, and the spec check
  applies the spec literally to find such a case. (PR 17 spec check N1)
- **A test that asserts the order of concurrent calls is flaky.** The implementer's garmin HTTP test compared the
  two device calls that `call_many` runs concurrently in call order; the first `check.sh` failed once, and the fix
  (compare sorted) was green in six repeated test stages. Concurrent calls are compared as a set or sorted; the
  checklist item on test independence now names it. (PR 17 impl 1)
- **A hand-written UTC offset in a test is right only for its dates.** The briefing and scheduler tests build
  device times with a fixed `+02:00` for Europe/Prague on 2026-10-07; they are correct (DST ends 2026-10-25) and
  say so in a comment, but moving the date past the change would turn them red without any code change. Prefer
  `ZoneInfo`, or keep the comment. (PR 17 impl 1, decision 5)
- **A new call path copies a guard and not its test.** `sync_check` has the same `anyio.fail_after(180)` as `fetch`,
  but only `fetch` had `test_a_hung_tool_is_bounded_by_the_overall_timeout`; the reviewer's mutant `if True:` survived
  1270 tests. A second survivor was the hold-back predicate `<` against `!=`, which needs one row with a sync
  dated after the briefing day. When a path is added next to a sibling, the sibling's guard tests are copied for it
  (new checklist item); the mutation table lists the `!=` form of every date comparison. (PR 17 review 1, O1, O2)
- **A prototype output that no longer matches its script is a designer artefact.** `coach_helpers.out` had 15 `parse`
  rows and two stale rows that the script did not print, so spec R4 ("15 rows with the expected value printed
  there") pointed at values nobody had run; the spec checker ran the real `parse_last_sync` and the implementer
  used those. The designer regenerates every `.out` from its script after the last edit and states the run command
  with the env it needs (`-e PYTHONPATH=/app/src`). (PR 17 spec check N5)
