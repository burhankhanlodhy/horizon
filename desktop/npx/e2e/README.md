# Live test for contextshrink (npx)

This test runs the real installer and one real Claude Code request on this PC. It
uses a throwaway Claude Code folder, so your real `~/.claude` is not touched. Use it
when the ContextShrink desktop app is also installed here.

The script is `run-live-test.ps1`. It runs in Windows PowerShell 5.1 or PowerShell 7.

## Before you start

You need:

- Node 18 or newer.
- Claude Code, with the `claude` command on PATH.
- A ContextShrink account. Email verification must be done if the account needs it.

You type your email and password yourself at the connect prompt. The script does not
read, log or print them. It also never prints an account key, a header or a settings
value.

## Steps

1. Open PowerShell. Go to the `desktop\npx\e2e` folder of the repository.
2. Run the dry run first. It sends nothing and changes no settings:

   ```
   powershell -NoProfile -ExecutionPolicy Bypass -File .\run-live-test.ps1 -DryRun
   ```

3. If the dry run passes, run the real test:

   ```
   powershell -NoProfile -ExecutionPolicy Bypass -File .\run-live-test.ps1
   ```

4. At the `Email:` and `Password:` prompts, type your ContextShrink details.
5. If the script asks to run `claude auth login`, answer `y`. Your browser opens. Sign in
   to Claude there. This login is only for the test folder.
6. Wait for the single headless request. The script prints only whether it worked, the
   model, the input and output token counts, and the cost if one is reported.
7. At the cleanup question, answer `N` to only disconnect, or `y` to also revoke this
   computer's key. If you chose `y`, type your password again.
8. Answer `y` or `N` to delete the test folder. The folder may still hold your Claude
   Code login, so deleting it is the safer choice.
9. Open the ContextShrink dashboard. Find the newest request, from the last few minutes.
   Check two things:
   - Its agent is `claude-code`.
   - Its Transforms do not contain `router:tool_search_deferral`.

## What the script does

- It creates a folder named `cs-npx-e2e-<date and time>` under `%TEMP%`.
- It points `CLAUDE_CONFIG_DIR` at that folder, and `HOME` and `USERPROFILE` at a
  folder inside it. These are set only for the processes this script starts.
- It removes `ANTHROPIC_BASE_URL`, `ANTHROPIC_CUSTOM_HEADERS`, `ENABLE_TOOL_SEARCH`,
  `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `CLAUDECODE` and any `CLAUDE_CODE_*`
  variable from those processes, if your terminal has them.
- It records the hash of your real `~/.claude\settings.json` before the run. It
  compares the hash again at the end. If the file changed, the script says so and
  exits with an error.
- It restores its own environment changes when it finishes.

## What you should see

- The dry run prints the folder, the Node and Claude Code versions, and the isolation
  checks. It ends with "dry run checks passed".
- The real run prints "request succeeded", then the model and token counts.
- The settings check confirms that the base URL host is `proxy.contextshrink.com`, and
  that the account key header and `ENABLE_TOOL_SEARCH=true` are present.
- At the end, the real settings check says the real `~/.claude\settings.json` is unchanged.

## If something goes wrong

- If connect fails, the script restores the test folder by itself, without a password.
  If an account key was made, revoke it on the API Keys page of the dashboard.
- If the headless request reports an authentication error, run
  `claude auth login` with `CLAUDE_CONFIG_DIR` set to the test folder, then run the
  script again.
- If the output was not JSON, the script says so and does not print it.
- If the real settings check fails, stop and tell the developer before you do anything else.

## Notes for the developer

- The headless command is `claude -p "Reply with the single word: pong" --output-format json`.
  These flags were checked with Claude Code 2.1.295 (`claude --help`).
- Sign-in uses `claude auth login` (subscription login by default), and login state is
  read with `claude auth status --json`. Only the `loggedIn` flag is read.
- The npx tool keeps its own record at `~\.contextshrink\npx.json`. The test points
  `HOME` and `USERPROFILE` at the test folder, so that record stays out of your real home.
- The script removes `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN` for the test
  processes. Otherwise the request could use an API key instead of the test folder's login.
- The JSON reader expects the usual Claude Code result fields. If a field is missing,
  the script prints "unknown" for it rather than failing.
- Not checked yet: whether Claude Code keeps its login in the config folder on Windows
  (it is expected to) and whether the proxy accepts that login. The dashboard check is
  the test for the second point.
