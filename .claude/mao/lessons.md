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

## Operational lessons
