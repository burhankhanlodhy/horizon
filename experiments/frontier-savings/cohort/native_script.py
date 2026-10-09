"""Zero-cost native Bash permission check using a reviewed fixture-only script.

This is a mechanics check, not an economic benchmark: the script is supplied by
the harness. No bypass mode, real login, external provider or global settings.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("native_fixture", HERE / "native_claude.py")
native = importlib.util.module_from_spec(spec)
spec.loader.exec_module(native)
OriginalScenario = native.Scenario

# Preserve the source block directly; AST positions locate the template/table.
SCRIPT = (
    "import ast;from pathlib import Path;p=Path('catalog.py');s=p.read_text();"
    "t=next(n.value for n in ast.parse(s).body if isinstance(n,ast.AnnAssign) "
    "and isinstance(n.target,ast.Name) and n.target.id=='ANTHROPIC_PRICES');"
    "i=next(i for i,k in enumerate(t.keys) if k.value=='claude-3-5-sonnet-latest');"
    "lines=s.splitlines(keepends=True);k=t.keys[i];v=t.values[i];"
    "b=''.join(lines[k.lineno-1:v.end_lineno]);"
    "pos=sum(map(len,lines[:t.end_lineno-1]))+t.end_col_offset-1;"
    "new=''.join(b.replace('claude-3-5-sonnet-latest','fixture-sonnet-'+str(j)) "
    "for j in range(4));p.write_text(s[:pos]+new+s[pos:])"
)
COMMAND = f'"{Path(sys.executable).as_posix()}" -I -c "{SCRIPT}"'


class ScriptScenario(OriginalScenario):
    def respond(self, body):
        self.requests.append(body)
        for message in body.get("messages", []):
            for block in (
                message.get("content", []) if isinstance(message.get("content"), list) else []
            ):
                if block.get("type") == "tool_result":
                    self.results[block["tool_use_id"]] = block
        if "read_cohort" not in self.results:
            call = {"id": "read_cohort", "name": "Read", "input": {"file_path": str(self.path)}}
        elif "script_cohort" not in self.results:
            call = {"id": "script_cohort", "name": "Bash", "input": {"command": COMMAND}}
        else:
            return self.message([{"type": "text", "text": "DONE"}], "end_turn")
        return self.message([dict(call, type="tool_use")], "tool_use")

    def summary(self, returncode, timed_out):
        result = self.results.get("script_cohort", {})
        actual = self.path.read_text()
        equal = ast.dump(ast.parse(actual)) == ast.dump(ast.parse(self.edit.after))
        return {
            "client_exit_code": returncode,
            "passed": bool(equal and result and not result.get("is_error") and not timed_out),
            "ast_matches": equal,
            "script_result_received": bool(result),
            "script_rejected": bool(result.get("is_error")),
            "requests": len(self.requests),
            "external_model_calls": 0,
            "external_api_spend_usd": 0,
        }


def main():
    native.Scenario = ScriptScenario
    # Adjust only the disposable client's tool catalog and scoped allow rules.
    original_run = native.subprocess.run

    def run(command, **kwargs):
        if "--tools" in command:
            command[command.index("--tools") + 1] = "Read,Edit,Bash"
            command[command.index("--allowedTools") + 1] = "Read,Bash"
            command.extend(["--permission-mode", "dontAsk"])
        return original_run(command, **kwargs)

    native.subprocess.run = run
    root = (
        HERE
        / "runs"
        / ("script-permission-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
    )
    root.mkdir(parents=True)
    binary = (
        Path(os.environ["APPDATA"]) / "npm/node_modules/@anthropic-ai/claude-code/bin/claude.exe"
    )
    result = native.run_case(binary, root, "native_control")
    (HERE / "native_script_results.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
