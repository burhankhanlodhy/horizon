# Coding comparison: Claude Desktop Code tab, plain against Vachi

Run the same coding work twice, in the Code tab of Claude Desktop:

- **Run A** in `runs\A-plain`, with Vachi off.
- **Run B** in `runs\B-vachi`, with Vachi on.

Then compare what each run cost and whether the code works. Each run is 10
tasks in one session and takes about 30 to 60 minutes.

## Rules that keep it fair

- **Use the same model and effort in both runs.** Pick them in the Code tab
  before task 1, for example Opus at Medium, and don't change them.
- **Use nothing else that bills to your Claude account during a run.** That
  means no Chat tab, no other Code sessions, no terminal `claude`, and no other
  devices. Tell the Claude helping you with ContextShrink that you're starting,
  so it stays idle.
- **Paste the tasks from `TASKS.md` exactly, one at a time.** Wait for each to
  finish. If Claude asks something, reply only `Use your best judgment and
  continue.` If it stops early, reply only `Continue.`
- **Don't help, don't run `/compact` or `/clear`, and don't edit the files
  yourself.**
- **Approve permission prompts the same way in both runs.** Each run folder
  already allows reading and editing files, `npm test`, `node` and `git diff`,
  so there should be few prompts.

## Run A: Vachi off

1. Open a terminal in this folder (`VachiCompare`) and turn Vachi off:
   ```
   npx vachi-claude disable
   ```
2. **Fully quit Claude Desktop:** right-click its tray icon and choose Quit.
   Then reopen it.
3. Check the route:
   ```
   node route.js
   ```
   It must say `Vachi routing: OFF`. If it says ON, stop and tell Claude.
4. Note the **account usage total** (the same total you read in the earlier
   tests) in `RESULTS-SHEET.md`.
5. In Claude Desktop, open the **Code** tab, start a **new session** in
   `VachiCompare\runs\A-plain`, and set the model and effort.
6. Paste tasks 1 to 10 from `TASKS.md`, one at a time.
7. When task 10 is done, **wait one minute**. Then note:
   - the account usage total;
   - the session's usage panel: cost, input, output, cache read, cache write;
   - the time.
8. Grade it:
   ```
   node check.js runs\A-plain
   ```
   Copy the two pass counts into the sheet.

## Run B: Vachi on

1. Turn Vachi back on, and approve the sign-in again if it asks:
   ```
   npx vachi-claude
   ```
2. Fully quit Claude Desktop from the tray and reopen it.
3. `node route.js` must say `Vachi routing: ON (terminal: on, Code tab: on)`.
4. Note the account usage total.
5. Open the **Code** tab and start a **new session** in
   `VachiCompare\runs\B-vachi`, with the same model and effort as run A. The
   Vachi status chip should show.
6. Paste tasks 1 to 10 the same way.
7. Wait one minute, then note the same numbers as in run A. Also note what
   `/vachi-savings` claims, in the sheet's "Anything unusual" row.
8. Grade it:
   ```
   node check.js runs\B-vachi
   ```

## What to send back

- `RESULTS-SHEET.md`
- `results.jsonl`, written by `check.js`

Neither file contains your code or prompts.

## How the result is read

- **Cost:** the run cost from the account usage total, B against A. The session
  panel is only a cross-check: in run B its numbers pass through Vachi's
  gateway, while the account total comes from Anthropic.
- **Quality:** the 29 hidden acceptance tests. A cheaper run with fewer passing
  tests is not a win.
- **It's one pair of runs.** Sessions vary from run to run, so only a large gap
  means much. For a firmer answer, repeat it another day in the opposite order,
  B first, using a fresh kit (`node setup.js --out` with a new folder).

## Notes

- `check.js` runs the code Claude wrote, with Node, on this computer.
- The hidden tests live in `_grader`. Don't open that folder in a Code session.
- To remove Vachi completely afterwards:
  - run `npx vachi-claude disable`;
  - delete the `VachiAI` task in Task Scheduler;
  - your old Claude Code settings are in `%USERPROFILE%\.vachi\backups`.
