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
