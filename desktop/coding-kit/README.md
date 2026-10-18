# Coding comparison kit

A fixed coding task for comparing Claude Desktop's Code tab with and without a
request gateway such as Vachi. Each run does the same 10 tasks in one session,
and the result is the usage cost of each run plus the hidden acceptance tests
it passes.

| Path | What |
|---|---|
| `template/` | The starting project: a small CommonJS Node library with a seeded bug and 6 tests (one fails). No dependencies. |
| `TASKS.md` | The 10 prompts, pasted one at a time. |
| `grader/acceptance.test.js` | 29 hidden acceptance tests for the finished project. |
| `check.js` | Grades a run folder and appends the result to `results.jsonl`. |
| `route.js` | Shows whether Claude Code is routed through Vachi. It prints hosts and setting names only. |
| `GUIDE.md`, `RESULTS-SHEET.md` | The owner's steps and the sheet for the readings. |
| `reference/` | A reference solution, used only by `selftest.js` and never copied into a kit. |
| `setup.js` | Creates the kit in `%USERPROFILE%\VachiCompare`, outside this repository, with two identical git-initialised run folders. |

Check the kit:

```
node desktop\coding-kit\selftest.js
```

Create a kit:

```
node desktop\coding-kit\setup.js
```

It needs Node 18 or newer and nothing else; git is used if present.
