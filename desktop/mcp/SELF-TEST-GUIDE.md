# ContextShrink self test: plain guide

This test answers one question for your own Claude Enterprise account: does
ContextShrink make your Claude Desktop work cheaper without making the answers
worse? You run everything yourself. Cost comes from the usage numbers Claude
Desktop already shows you.

Expect about 8 measurement days of 1-2 hours each, and probably $5-15 of
usage in total.

## The four methods

Every task is done four times, once per method, on different days.

| Method | In Claude Desktop |
|---|---|
| **A** ContextShrink, compression off | ContextShrink connector **on**, extension *Pilot method* = `exact_only` |
| **B** ContextShrink | ContextShrink connector **on**, *Pilot method* = `normal` |
| **C_normal** Plain Claude | ContextShrink connector **off**; attach the files with the paperclip |
| **C_tuned** Plain Claude, concise | Same as C_normal; the wizard's prompt starts with ContextShrink's short answer rules |

Use the **same model and effort setting** on every day.

## One-time setup

1. **Install extension 0.1.2.**
   1. Fully quit Claude Desktop from the tray.
   2. Remove the old ContextShrink in Settings → Extensions.
   3. Install `contextshrink-desktop-0.1.2.mcpb`.
   4. Set your approved folder to a folder that holds your task documents.
2. **Check the Pilot method switch works.**
   1. Set *Pilot method* to `exact_only`.
   2. Fully quit and reopen Claude Desktop.
   3. Ask Claude "show ContextShrink stats". It should report `evaluation_arm: raw`.
   4. Set it back to `normal`, restart, and check that it now says `optimized`.

   Do this each time you change it.
3. **Write your task pack.** Copy `desktop/mcp/self-test/pack.template.json`
   and replace every example.
   - **16 tasks in 8 groups of 2.** Each task is a question about one or more
     of your documents, with one clear right answer.
   - **Answer types:**
     - `number`, for example a total, with an optional tolerance;
     - `text_contains`, for example "90 days" (whole words must appear);
     - `date`;
     - `choice`, a list of accepted answers.
   - **Use a good mix:**
     - a few JSON or log files (where compact mode helps);
     - long documents with specific questions (where focused mode helps);
     - a couple of small files (where nothing should help, which is an honest
       control).
   - **Pair tasks of similar size** in a group, so each group costs a few cents
     or more. The meter only shows whole cents.
   - Set `model_label` to the model and effort you will use, for example
     "Claude Opus at standard effort".
4. **Start the study.**

   ```powershell
   & 'C:\Users\Burhan\OneDrive\Desktop\ContextShrink\.venv\Scripts\python.exe' desktop\mcp\self_test.py init --pack my-pack.json
   ```

   This prints your 8-day plan. The method order is shuffled, so do not change
   it.

## Each measurement day

Run this one command and follow what it says. You can stop with `q` at any
time and run it again to continue.

```powershell
& 'C:\Users\Burhan\OneDrive\Desktop\ContextShrink\.venv\Scripts\python.exe' desktop\mcp\self_test.py run
```

The wizard will:

1. **Show today's method setup.** Set Claude Desktop that way (restart it if
   you changed *Pilot method*), then type `yes`.
2. **For each group,** ask for your usage total from Settings → Usage, for
   example `20.23`.
3. **For each task,** show the prompt (press `C` to copy it). Start a **new
   chat** for every task, paste the prompt, attach files if the method says
   so, and wait for the whole reply. Then paste the line that starts with
   `ANSWER:`.
4. After the group, **make you wait 2 minutes**, then ask for the usage total
   again.

Rules for measurement days:

- Only one measurement day per calendar day, finished on the same day.
- **No other Claude use while a group is running.** That includes other
  Claude Desktop chats, and Claude Code or Codex if they bill to this
  account. Tell Claude Code you are measuring so it stays idle.
- Avoid other Claude Desktop use for the rest of the day, if you can. It
  shows up in the daily cross-check.

## About 4 hours after each day

Run the same `run` command, or this one:

```powershell
& 'C:\Users\Burhan\OneDrive\Desktop\ContextShrink\.venv\Scripts\python.exe' desktop\mcp\self_test.py reconcile
```

It asks for that day's **Claude Desktop** line from the daily breakdown by
product in Settings → Usage. If it doesn't match your readings, the day is
marked and you will redo it on another day.

## At the end

```powershell
& 'C:\Users\Burhan\OneDrive\Desktop\ContextShrink\.venv\Scripts\python.exe' desktop\mcp\self_test.py report --output self-test-report.json
```

The report shows, for each method:

- the total cost;
- the number of correct answers;
- the cost per correct answer.

It also gives a verdict for **ContextShrink (B) vs plain Claude (C_normal)**:

| Verdict | Meaning |
|---|---|
| `savings_supported` | B was cheaper, with enough clean data to be confident, and its answers were not worse |
| `inconclusive` | No clear difference |
| `cost_increase_supported` | B cost more |
| `none` | Not enough clean data, or B's answers were worse; the reasons are listed |

Every verdict applies to **your account, this model setting and these tasks
only**. It is evidence for you, not a general promise.

Use `status` at any time to see what is done and what is next:

```powershell
& 'C:\Users\Burhan\OneDrive\Desktop\ContextShrink\.venv\Scripts\python.exe' desktop\mcp\self_test.py status
```

## Good to know

- **Your data stays local.** The wizard stores your readings and whether each
  answer was right, in `%LOCALAPPDATA%\ContextShrink\self-test\<study_id>`. It
  never stores answer text or document contents.
- **Method A reads in pages.** With compression off, long files come back in
  pages, so Claude has to fetch the next pages to read everything. That is
  the intended baseline.
- **You'll learn the answers.** That is expected. The prompts are fixed and the
  method order is shuffled across two rounds, so learning affects all methods
  roughly equally.
