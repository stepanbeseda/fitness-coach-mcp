# How this was built

The project was built in about two weeks, as 27 planned PRs on top of an initial skeleton (22 features, then 5
refactors and fixes from two architecture reviews; 3 more are planned), mostly by Claude Code agents working to
a written plan, with me as the owner who decides, reviews and deploys. This page explains the process; the
artefacts it refers to are all in the repository.

## 1. A written plan before code

[`development-plan.md`](development-plan.md) is the source of truth:

- **Decisions and boundaries** (section 1): what the system is and is not (single-user, no web UI, health data
  stays on the server), one way in through the gateway, curated tools, confirm-before-write, log privacy, a
  hash-locked supply chain. Every PR is checked against these.
- **Common PR conditions** (section 2): one PR per chapter, docs in the same PR, a test for every acceptance
  criterion that can be verified offline, and criteria that need the real Garmin or Hevy account marked "on
  the test host".
- **One brief per PR** (section 4): the problem, the design, the acceptance criteria and the files involved.
  Chapters are never rewritten; a "Delivered" block is appended when the PR lands, so the plan doubles as a
  record of what was built and why.

## 2. Agents with separate roles

Each PR goes through the same loop, configured in [`.claude/mao/project.md`](../.claude/mao/project.md):

1. **Designer** turns the brief into a spec: exact tool names and payload shapes, the tests to write, the docs to
   touch.
2. **Worker** checks the spec against reality before any code is written, for example that every
   `python-garminconnect` method it names exists in the installed library version.
3. **Implementer** writes the code and tests in its own git worktree, so several PRs can be built in parallel.
4. **Reviewer** reviews the diff against the plan's rules and the checklist, and accepts only a
   `scripts/check.sh` run whose `tree=` hash matches the reviewed tree. That proves the green check belongs to
   exactly that code.
5. **Owner (me)**: anything outside the repository is stop-and-ask. That covers deploys to the live stack,
   secrets, deleting data, and any call that writes to my real Garmin or Hevy account.

The agents run on the same host as the live stack, because `scripts/check.sh` needs Docker. Docker access is
root-equivalent: a container an agent starts could mount `secrets/` or the data volumes. So the forbidden list and
the stop-and-ask rules in `project.md` are process guards, backed by Claude Code's own approval prompts; they are
not a security boundary. A real boundary would be a separate machine without the live secrets, which this project
does not use. The committed [`.claude/settings.json`](../.claude/settings.json) adds deny rules for `.env`,
`secrets/` and `data/` as defence in depth. In a probe with Claude Code 2.1.302 they stopped the Read tool on all
three paths, the Edit tool on `.env`, the Write tool in `secrets/` and a direct shell read or write such as
`cat .env`, but a command that reached the files another way, such as `grep -r` over the repository or a script
that builds the path, still read them.

## 3. Mistakes become rules

Review findings that repeat do not stay in review comments:

- [`checklist.md`](../.claude/mao/checklist.md) (43 items) is what the implementer self-reviews against and
  the reviewer works through, each item marked ok / finding / n/a.
- [`lessons.md`](../.claude/mao/lessons.md) (80+ entries) records the *shape* of each repeated error with the PR
  it came from, and every agent reads it before starting.

One example. In PR 01 a review found a branch in a payload shaper that no test noticed when it was forced
(`running = True`, suite still green). That became a lesson and a checklist item: every conditional in a
shaper needs a canned payload that takes it, and the self-review mutates each branch once.

## 4. Verification in layers

- **Offline tests** (more than 1,100 test functions) with a fake Garmin client and canned payloads; no test
  touches the network. The last line of `scripts/check.sh` (`CHECK OK ... garmin=<n> gateway=<n> coach=<n>`)
  gives the exact number of passing tests per service.
- **Static checks** (ruff and mypy) in each service's Docker test stage, so an image cannot be built from code
  that fails them.
- **End-to-end**: `scripts/check.sh` builds every image and runs the whole OAuth 2.1 + MCP flow in a throwaway
  Compose project with generated secrets, without touching the live deployment. CI runs the same.
- **Live verification**: criteria marked "on the test host" are checked against my real deployment with
  `scripts/smoke-test.sh`, after I approve the deploy.

## What I took from it

- Agents are fast at code and slow to notice what they have not been told. The leverage is in the plan's
  boundaries, the checklist and the lessons file, which turn one review finding into a rule for every later PR.
- Evidence beats assertion. "Tests pass" means little until the check output is tied to a tree hash.
- Human approval belongs exactly where the actions cannot be undone: deploys, secrets and the athlete's real
  data.
