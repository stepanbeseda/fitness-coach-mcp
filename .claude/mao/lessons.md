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
  review 1, N6)
