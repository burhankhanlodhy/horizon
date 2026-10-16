# ContextShrink self test: one-day guide

This test answers one question for your own Claude Enterprise account: does
ContextShrink make Claude Desktop work cheaper without making the answers worse?
You run everything yourself. Cost comes from the usage numbers Claude Desktop
shows you.

Plan for one day: about 3 to 4 hours of work, then one check about 4 hours
after your last reading. You will run 48 chats in total, with 12 tasks, each
done once with each of the four methods. A pack may list a subset with
`"methods": [...]` (it must include `B` and `C_normal`); then the phases,
chats and report cover only those methods. The laptop kit uses `B,C_normal`:
12 tasks, 24 chats, 4 phases.

On a laptop without Python, use the portable kit instead of the commands below.
Build it with `python desktop/mcp/self-test/build_kit.py`. It writes
`desktop/mcp/extension/dist/ContextShrink-SelfTest-<version>.zip` with the
generated documents, a launcher (`Start Self Test.cmd`) and its own short
`GUIDE.txt`.

**What the test can show.** ContextShrink saves money only on files Claude
reads through it, and most when a chat keeps asking about large files. It does
not change ordinary chatting, small attachments, or code that Claude writes.
See "When ContextShrink helps" in `extension/README.md`. A result from this test
applies to chats like the test, not to every chat.

## Before the day

1. **Install extension 0.1.3 on the test laptop.** Fully quit Claude Desktop from
   the tray, remove the old ContextShrink in Settings → Extensions, install the
   `.mcpb` file, and approve the folder that holds your task documents.
2. **Write your pack.** Copy `desktop/mcp/self-test/pack.template.json` and
   replace every example. You need 12 tasks, one per group, each with one clear
   right answer. Set `model_label` to the model and effort you will use, for
   example "Claude Opus at standard effort". Keep that setting for the whole day.
3. **Start the study.** From the folder with the test files:

   ```powershell
   python -m horizon.desktop_mcp self-test init --pack my-pack.json
   ```

   It prints the plan (8 phases, 48 chats). Do not change the plan.

## During the day

Start the run and follow the screens:

```powershell
python -m horizon.desktop_mcp self-test run
```

The run works in **phases**. Each phase is one method, Claude Desktop set up
one way, and the wizard shows:

- **"Phase N of 8: method X"**, with a setup checklist.
- **Step 1:** type the running total now, before you change anything.
- **Step 2:** set Claude Desktop up as the checklist says. For methods A and B,
  the ContextShrink check chat ("show ContextShrink stats") happens now, before
  this phase's first group. Its spend is expected and is not measured.
- Then type `yes`.

For each group the wizard then does this:

1. Asks for the total (**before**). Close other Claude chats first.
2. Shows the prompt. Press `C` then Enter to copy it. Start a **new chat**, paste
   the prompt, attach files if the method says so, and wait for the full reply.
3. Asks you to paste the line that starts with `ANSWER:`. Type `V` instead to
   void the group; it is redone straight away.
4. Waits **90 seconds**, then asks for the total again (**after**).

Rules for the day:

- **Use no other Claude product all day.** No other Claude Desktop chats, no
  Claude Code, no Codex. Anything else on this account lands in the same
  running total and makes the check fail.
- Restarts happen only when the Pilot method changes. The checklist says so.
- You can stop at any prompt with `q`. Run the same `run` command later to carry
  on; the wizard asks about any group left open.
- If you run past midnight, the wizard warns you when less than 4 hours remain.
  You can pause and finish tomorrow: each date is checked separately.

## About 4 hours after your last reading

Run this, or run `run` again, which also checks dates when they are due:

```powershell
python -m horizon.desktop_mcp self-test reconcile
```

It asks for the **Claude Desktop** line for each date from the daily breakdown by
product in Settings → Usage. Type it twice, as it asks. If it matches your
readings, the date counts. If it does not match, the groups on that date are
left out of the results.

## At the end

```powershell
python -m horizon.desktop_mcp self-test report --output self-test-report.json
```

The report gives, for each method, the total spend, the number of correct
answers and the cost per correct answer. It also gives a verdict for
**ContextShrink (B) vs plain Claude (C_normal)**:

| Verdict | Meaning |
|---|---|
| `savings_supported` | B was cheaper, with enough clean data, and its answers were not worse |
| `inconclusive` | No clear difference |
| `cost_increase_supported` | B cost more |
| `none` | Not enough clean data, or B's answers were worse; the reasons are listed |

It also shows the time each phase started and ended, the warnings, and any
groups left out, with reasons. Every verdict applies to **your account, this
model setting and these tasks only**.

Use `status` at any time to see progress and the next step:

```powershell
python -m horizon.desktop_mcp self-test status
```

## Good to know

- **Your data stays local.** The wizard stores readings and whether each answer
  was right, in `%LOCALAPPDATA%\ContextShrink\self-test\<study_id>`. It never
  stores answer text or document contents.
- **Warnings do not remove a group.** If Claude Desktop spent money between two
  groups of the same phase, the wizard lists a warning and keeps the group.
  Spend at a phase boundary is expected, because of the setup check.
- **Method A reads in pages.** With compression off, long files come back in
  pages. That is the intended baseline.
- **You'll learn the answers.** That is expected. The prompts are fixed and the
  method order is shuffled, so learning affects all methods roughly equally.
