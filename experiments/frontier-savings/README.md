# Frontier savings research

Read [REPORT.md](REPORT.md) for the five methods, primary sources, local results,
limitations and recommended next experiments. Completed October 9, 2026.

The original five prototypes run offline; the follow-up edit pilot uses live
model calls. Numbers labeled modeled or oracle are not observed
provider savings and must not enter billing. See the report's reproduction
commands; each method keeps its code and results in its own directory.

Follow-up work:

* [Compact-edit pilot](pilot/README.md): preview-only MCP adapter, local guards,
  live evaluation harness and [measured results](pilot/RESULTS.md).
* [Automatic proxy framework](pilot/PROXY_AUTOMATION_FRAMEWORK.md): automatic
  eligibility, native tool translation, cache/replay and cost/accounting gates.
* [Large cold restores](context_slice/COLD_RESTORE.md): private local replay of
  100k/250k/500k history windows, economic gates and quality limitations.
