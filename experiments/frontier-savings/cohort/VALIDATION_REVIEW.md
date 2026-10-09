# Compact edits: validation and rollout decision

October 9, 2026. Branch: `codex/compact-edit-adapters`.

## Decision

**Keep this cohort unqualified for automatic customer activation.** The native
script comparison now works. Compact edits reduce the edit payload and latency
on these fixtures but cost more for the complete workflows under observed cache
conditions. Even the cold-cache sensitivity fails the controller's minimum
$0.003 and 15% saving against scripts. The earlier single-pair 18.1% result is
superseded as a rollout basis. No account savings, billing or production
qualification was changed. This result does not rule out other compact cohorts.

## Completed work

1. Integrated main's `38b966e` and subsequent `33ac180` accounting corrections
   into the feature branch. The feature was not merged into main.
2. Checked normal Bash permissions with installed **Claude Code 2.1.295**.
   The earlier live attempt used a compound multiline command and was denied.
   A reviewed standalone script passes using ordinary `dontAsk` permissions and
   explicit Read/Bash allow rules. No permission bypass or global settings.
   This establishes a functioning baseline, not one proven denial cause.
3. Completed twelve live workflows: native tool choice, model-written scripts
   and compact edits, four/twelve clones, two repetitions per arm/size. Four
   supplemental workflows cover thinking encouragement, unused native/compact
   catalogs and signed-thinking replay. **16/16** match expected AST/comments;
   zero permission denials. Scripts were available in every arm. The script
   control generated its own script, without a supplied compiler/helper.
4. Ran installed Claude through actual `create_app()` authorization, normal
   compression/prefix/cache handling, scripted provider HTTP and SQLite outbox.
   Compact execution/replay: three provider attempts, three outbox rows, actual
   native result acknowledged. Invalid private preview: four attempts, four
   rows, one native retry; no fictitious virtual acknowledgement/replay.
   Both checks pass with zero paid calls. Production lifespan workers and
   external outbox delivery acknowledgement are outside this harness.
5. Added durable admission-stop, source retirement, drain status and consistent
   SQLite backup controls, with eight new regression cases. An abrupt child
   process exit after durable publication retains the reservation without a
   close/checkpoint or inferred client result. This is journal crash evidence,
   not a production service-kill/recovery test.
6. Audited Pi 5's Docker deployment, checked health, preserved its rollback
   image/config and verified imports in an isolated container. No restart.

## Economic evidence

These are **modeled list-price dollars from returned usage**, using official
Sonnet 5.5 rates: input $2/M, output $10/M, five-minute write $2.50/M, one-hour
write $4/M, read $0.10/M. Thinking is included in output, not counted twice.
Every attempt reported standard tier/global inference and complete write-TTL
splits. [Anthropic pricing](https://platform.claude.com/docs/en/about-claude/pricing).

| Clones | Native tool choice | Script control | Compact | Compact cost increase vs native |
|---|---:|---:|---:|---:|
| 4 | $0.027752 | $0.024549 | $0.030085 | 8.4% |
| 12 | $0.023213 | $0.023342 | $0.031712 | 36.6% |

Each cell is the mean of two complete workflows, including Read and final
completion. Native choice selected Edit for both four-clone tasks and Bash for
both twelve-clone tasks. Compact chose the virtual tool for all four main tasks.
Mean client latency: native 9.10/9.09 seconds, scripts 7.52/8.99, compact
6.13/7.37 (four/twelve clones). These are small sequential samples, not guarantees.
Paid traffic uses the bounded cohort relay; stock-proxy execution has separate
scripted evidence. No overall proxy/customer saving percentage is established.

### Cache confounding and sensitivity

Order is balanced across two repetitions, but cache conditions are not matched.
Most native/script first requests reused 2,513 cached tokens; compact first
requests had zero cache reads. Its unique session receipt appears in the tool
schema, changing the provider tool prefix between sessions. That is a deployment
risk, not proof of the cause of every miss. Later turns in all arms have reads.

`validation_analysis.py` also prices each first-request cached prefix as a cold
one-hour write. This is an arithmetic sensitivity, not a cold rerun. Against
scripts, compact then costs **2.2% more** for four clones and saves
**4.3%/$0.00143** for twelve. Neither passes admission. Two repetitions on one
literal-clone workload cannot establish reliable whole-session cost bounds.

The single unused-catalog pair costs $0.014146 native vs $0.025694 compact after
official repricing. This also differs in cache warmth; it illustrates the need
to include unused admissions, not a universal overhead percentage.

### Budget and tariffs

The additional allocation started at $5 and, under the user's authorization to
increase it, its conservative reserve ceiling became $6 for the final protocol
check. **47** returned attempts across **16** workflows remain in the ledger.
Conservative usage upper: **$4.447108**. CLI API-equivalent quote:
**$0.4447108**. Official-rate modeled cost: **$0.4271124**. No invoice reconciliation.
The CLI quote uses the older $0.20/M read price and is retained separately.

Each call reserves a byte/input and maximum-output envelope before HTTP;
ambiguous outcomes retain escrow. No automatic provider retries. The previous
$3 ledger and unknown OneProvider charge were not reset or reconciled by this
new allocation. Raw headers/authentication are neither printed nor committed.

## Real signed thinking

Simple compact tasks emitted no thinking, including the first encouraged probe;
absence is not signed-replay evidence. A separate modular-reasoning probe
returned a real signed thinking block alongside Read. Claude replayed that exact
block through the compact request and completion after native Edit. The relay
checked its whole-block fingerprint; Anthropic accepted both continuations and
the edit completed. Raw blocks/signatures are not committed. This certifies this
bounded relay probe, not every model/transport or signed thinking through the
stock proxy. [Thinking guidance](https://platform.claude.com/docs/en/build-with-claude/thinking-steering-and-cost).

## Operational controls and limits

- `ReplayJournal.stop_admissions()` stores a durable irreversible latch for that
  journal generation. New admissions remain native; new managed registrations
  reject. Existing catalog/replay stays available subject to its original
  route/policy/qualification guards. No customer toggle or operator HTTP endpoint.
- `drain_status()` exposes hashed scopes and unpublished, pending-client-result
  or acknowledged states without source/arguments. Acknowledged includes actual
  permission errors: it proves receipt, not successful editing or session end.
  An unpublished operation can still have an admitted catalog. Preserve its
  continuation adapter. Pending delivery never permits automatic retransmission.
- Native Edit/Write/Bash or unknown tools retire the candidate, conservatively
  including harmless shell commands. Read/Glob/Grep do not. Journal-mapped
  compact Edit calls are exempt. Retirement persists despite later reads,
  retains the original schema/history and prohibits another compact edit.
  New candidates require a new certified conversation boundary.
- `backup()` uses SQLite backup to include committed WAL state, checks integrity
  and refuses overwrite. Operators must secure directories/backups containing
  source/native arguments. The backup is not an active-session rollback approval.
- Actual result replay provides reconciliation. There is no operator command to
  invent success or reset a reserved call. Live state is never deleted/evicted.
  Safe deletion still needs a managed-client terminal manifest and late-resume
  rejection; retention is the current safe behavior.

## Pi 5 deployment and rollback

Final audit: clean main checkout `/home/raspberrypi5/horizon` at
`33ac18036cb5a1014b94169c49fcfb1f0683d52b`, but running version still
`source-build+g38b966e160ab`. Image:
`sha256:62ff60419ef15bb1fabe8c9b3ef2a0559eb099b191de560310466839e720b8a0`.
Another main update arrived during validation; it was integrated into the
feature branch without rebuilding or restarting production.

Docker Compose project `horizon`, service `horizon-proxy`, container
`horizon-horizon-proxy-1`, host networking. `/livez`, `/readyz`, `/health` at
127.0.0.1:8787 returned 200 healthy/ready. API, Postgres and gateway were untouched.

Rollback tag: `horizon-proxy-rollback:pre-compact-38b966e-20261009`. Its ID matches
the running image. A temporary read-only container with no network/data mounts
imported the proxy successfully. This verifies an available image, not a tested
production restart/rollback. It is an on-host tag, not an offline image export
or protection from image deletion/pruning.

Owner-only directory
`/home/raspberrypi5/horizon-rollbacks/compact-edit-preflight-20261009` contains
compose, private environment/container inspection, manifest and image override.
Do not expose those private files. The `horizon_horizon_workspace` volume is
retained. There is no feature journal in the unchanged production deployment.

Future rollback must first stop new admissions and reconcile/drain managed
conversations. Restore backed-up config as needed and the pinned image with the
existing volume. Preserve the original project/directory and use
`--no-build --no-deps horizon-proxy`. Do not route admitted clients to old code
that cannot normalize their catalog/history. Review the exact future deployment
before executing this; no rollback/restart command was run here.

## Final regression evidence

**271 passed, two warnings, 52.56 seconds**, after integrating `33ac180`.
Ruff check/format and diff whitespace checks pass. Installed-client pipeline and
standalone-script checks were repeated on that branch and pass. The full suite
and production candidate build were not run.

Initial main pricing tests failed against the offline bundled LiteLLM catalog,
which lacks newer models. Loading the current upstream catalog and calling
`litellm.add_known_models()` before pytest resolved all failures. The final
runner uses this saved catalog. Production tariff code was not changed. Pin
catalog/dependency provenance for any future build; these tests do not certify
stale data. [LiteLLM catalog](https://github.com/BerriAI/litellm/blob/main/model_prices_and_context_window.json).

`regression_matrix.py` reproduces this targeted run using the existing ignored
snapshot. Its SHA-256 is
`e1160204c0513d78a1b22e7885f6862e0dea31698e206848c333f75d67b95b73`;
tested LiteLLM version is `1.103.0`. The runner refuses a different snapshot and
does not download a moving catalog. A fresh checkout must supply that exact
public snapshot or explicitly review/rebaseline its newer dependency metadata.

```powershell
.venv/Scripts/python.exe experiments/frontier-savings/cohort/regression_matrix.py
.venv/Scripts/python.exe experiments/frontier-savings/cohort/native_pipeline.py
.venv/Scripts/python.exe experiments/frontier-savings/cohort/native_script.py
.venv/Scripts/python.exe experiments/frontier-savings/cohort/validation_analysis.py
```

These commands do not call a paid model. `validation_claude.py --resume` is paid:
do not delete its ledger or add trials without checking the remaining reserve.

Evidence: `validation_results.json`, `validation_analysis.json`,
`native_script_results.json`, `native_pipeline_results.json`. Existing Claude
and Gemini ledgers remain intact. Raw fixtures, client output, journals and
fetched catalog are only under ignored `runs/`.

## Remaining activation prerequisites

Automatic production launcher/source collection and durable managed-client
manifest restoration are not integrated. The bridge remains an operator Python
API; request headers cannot create bindings/qualifications. Pi 5 cannot read
users' local files independently. A certified managed client must supply local
source capture, authenticated session/workspace binding, cold-boundary proof,
terminal manifests and restoration before accepting resumed traffic.

Codex stays native. Its wire adapters are mechanical only. Official hooks can
inspect/block/rewrite `apply_patch`, but are not a complete enforcement boundary.
Preflight hashing is separated from execution and cannot certify atomic
compare-and-apply against external writers/rename races. No
`atomic_client_sha256` qualification was minted.
[Codex hooks](https://learn.chatgpt.com/docs/hooks).

Production feature merge, candidate deployment/build, restart and live
service-kill/recovery checks were not executed because the economic gate failed.
This is not waiting for another permission confirmation. The cohort needs a
better demonstrated economic case first. Customer incremental credited savings
are zero, and no increased Pro fee is justified.

## Next design investigation

Retain the always-active controller and native fallback. Investigate a stable
virtual schema with candidate bindings outside the cached tool prefix while
preserving exact account/session/source checks. Benchmark it against generated
scripts before qualification. Test realistic larger edits where scripts cannot
cheaply express the same transformation: the current compiler supports only
4–16 literal ModelPricing clones and cannot claim general code-editing savings.
Include unused admissions, cache regimes, recovery and actual client guards.
