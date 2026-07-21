# Codex Time Machine: temporal information boundary

This repository implements the first backend primitive for Codex Time Machine: a
deterministic, sealed view of explicitly classified project evidence. A scenario
defines a controlled cutoff, task, network-policy requirement, and asset manifest.
The builder copies only assets that are both `AVAILABLE` and visible to
`PAST_CODEX`, records their SHA-256 hashes, and emits a canonical manifest and a
complete boundary report.

The implementation is an independently testable Python package. It has no API,
database, model call, retrieval layer, Ghost Engineer, or frontend.

## Boundary model

Positive inclusion is the security boundary: each Past-visible file must be
declared and eligible before it is copied. The builder never copies a repository
wholesale and then deletes known future files. This avoids leaks from forgotten,
untracked, generated, or newly introduced paths. Logical paths are checked for
absolute paths, traversal, case-insensitive collisions, and nested `.git`
components. Source paths containing `.git` are also rejected.

A Git checkout alone is insufficient because the evidence boundary can include
or exclude tests, documents, metrics, service snapshots, session artifacts, and
other files independently of Git history. Untracked files and external evidence
also have no reliable Git-time semantics.

The snapshot root hash covers stable scenario inputs, the canonical manifest of
materialized Past assets, and hashes of the canonical boundary-control artifact
and boundary report. It excludes `created_at` and all non-materialized file
contents. Consequently, identical Past evidence reproduces the same root hash,
while changes to locked future contents do not alter it. Changing a declared
availability or visibility boundary does change snapshot identity.

`sealed_snapshot/repo` is an immutable audited base project state, not a Codex
execution workspace. The run harness creates a fresh per-run workspace
from the same audited base for every baseline or Ghost replay. Test and build
execution can create `__pycache__`, coverage output, logs, generated files, and
agent edits; those are run-state mutations, not temporal-boundary evidence. The
base snapshot hash identifies only the starting project-information state.

## Guarantees and non-guarantees

The snapshot component freezes project evidence only. It does **not** rewind or
freeze the model's pretrained world knowledge. `network_policy: disabled` in a
scenario remains metadata; operating-system enforcement is supplied separately
by the run harness and must pass its local probe.

The initial `legalrag-reranker-t001` scenario is a `controlled_fixture` at fixture
revision `T0`. Its future-only artifacts are synthetic boundary-testing
placeholders, not Temporal Eval ground truth, scoring evidence, reranker-decision
evidence, or organic LegalRAG history. It is not represented as an organically
preserved historical Git state or a real historical date.

## Run

Install Python 3.11 or newer and the project dependencies:

```text
python -m pip install -e ".[dev]"
python -m pytest
python -m backend.temporal.snapshot backend/scenarios/legalrag_reranker_t001/scenario.yaml
```

The CLI builds `sealed_snapshot/` beside the scenario, then audits `.git`
absence, file hashes, declared visibility and availability, unmanifested files,
and the configured future canary.

## Temporal run harness

Phase 2 captures execution evidence from a fresh, non-resumed `codex exec`; it
does not interpret, infer, or score the agent trajectory. Before every run, the
harness audits the immutable `sealed_snapshot`, copies only
`sealed_snapshot/repo` into a new per-run `workspace/`, and verifies every
starting path and byte against the snapshot manifest. Control-plane files such
as `manifest.json`, `boundary_control.json`, and `boundary_report.json` never
enter the evaluated workspace.

The run manifest records three distinct identities:

- `base_snapshot_hash` identifies the audited project-information boundary.
- `workspace_start_hash` identifies the exact regular-file tree before Codex.
- `workspace_end_hash` identifies the resulting tree after Codex edits and
  generated run state.

The evaluated configuration is fixed to `gpt-5.6-sol` with medium reasoning,
approval policy `never`, and ephemeral execution. Evaluated runs use the beta
Codex permission-profile system and require Codex CLI 0.138.0 or newer; the
legacy `workspace-write` sandbox is not combined with it. The `ctm_temporal`
profile grants `:minimal` runtime reads, write access only to the active workspace
root, explicitly denies `:root`, and disables network access. It does not grant
the scenario tree, sealed
snapshot, runs root, home directory, or sibling paths. `--strict-config` makes
unsupported profile or configuration keys fail closed.

Preflight is time bounded (15 seconds per command by default) and parses the CLI
version before invoking any help command. Before the model process starts, the
harness runs a deterministic `codex sandbox` probe under the same profile with
`--include-managed-config`. It proves workspace read/write succeeds, sibling
read/write fails, a deliberately inherited environment canary is absent, and a
connection to a parent-owned ephemeral loopback listener is blocked. Configuration
state alone is not treated as enforcement proof. Managed requirements that reject
the profile fail the run rather than being bypassed.

The validated probe result, exact argument array, resolved executable/version,
working directory, raw stdout, and raw stderr are stored as separately hashed
control-plane evidence beside the run manifest, never inside the evaluated
workspace. Environment values are excluded from the command artifact. A failed
probe prevents model execution.

Web search is disabled at both the top-level and tool configuration. User
configuration and execpolicy rules are ignored; hooks, memories, remote-plugin
discovery, shell snapshots, multi-agent operation, goals, skill dependency
installation, and login shells are disabled.
`project_doc_max_bytes=0` disables automatic `AGENTS.md` project-document loading
for these controlled Phase 2 runs, and personality is set to `none`. Starting
workspaces containing any case-insensitive `.codex` path component are rejected.

The Codex parent process retains its host environment for executable discovery
and authentication. Model-generated commands receive a separate `core`
shell-environment policy with an explicit portability allowlist (`PATH`, platform
home/system/temp variables, `COMSPEC`, and `PATHEXT`). Authentication tokens,
cloud credentials, and scenario-specific variables are not included. Run state
defaults to the project-local `.ctm_runs/` directory outside the scenario tree;
only `.ctm_runs/<run-id>/workspace` is the active permission-profile workspace.

Codex JSONL stdout is preserved byte-for-byte in
`raw_codex_events.jsonl`; stderr and the final message are captured separately.
The harness validates JSONL and derives only event counts, event types, explicit
failure presence, observable item-type counts, and thread evidence. Success
requires exactly one `thread.started` event with a non-empty thread ID. Explicit
web-search or MCP item events fail the controlled run while preserving the raw
evidence. It does not capture or claim access to hidden reasoning.

The default Codex subprocess timeout is 1,800 seconds. Any stdout or stderr bytes
written before timeout are preserved exactly; either stream may validly be empty
if the process timed out before emitting data. Timeout, invalid JSONL,
failed-event, final-message hash, and workspace-end inspection paths all attempt
to write a terminal `SUCCEEDED` or `FAILED` manifest. A timeout may not clean up
an independently detached descendant process; stronger container-level process
isolation can address that limitation later.

Run the harness after creating the sealed snapshot. A real smoke run is valid
only after version/capability preflight and the deterministic isolation probe
pass. Passing fake tests is not real Codex acceptance evidence; a real smoke must
record a valid thread, zero web/MCP items, all base/workspace/event hashes, and all
probe evidence hashes:

```text
python -m backend.runs.runner backend/scenarios/legalrag_reranker_t001/scenario.yaml --run-id R-001 --kind BASELINE
```

This freezes project evidence, not the active model's pretrained world
knowledge. Operating-system and Codex CLI isolation capabilities are checked
locally and the run fails closed when required flags are unavailable. Managed
enterprise requirements may still impose controls outside this application's
authority, so their effect must be reported rather than described as disabled.

Runner-semantic tests use a fast in-memory adapter that writes deterministic
evidence directly. A small separate integration group executes the Python fake
CLI to cover real argument-list subprocess invocation, byte-exact stdout/stderr,
timeout handling, sandbox command execution, and one complete fake run.

## Observable trajectory extraction

Phase 3 converts a terminal `SUCCEEDED` run's immutable JSONL stream into a
normalized observable engineering trajectory. It correlates `item.started` and
`item.completed` by source item ID, emits one action for a completed item, keeps
started-only items as `INCOMPLETE`, retains failed commands, and expands each
declared file change into its own deterministic file event. Command tags are
conservative rules such as `test_execution`, `git_inspection`, and
`compilation`; no model is used for extraction or tagging.

Every normalized event records its raw JSONL line indexes, source item identity,
source event types, and a SHA-256 hash of the canonical source fragments. File
paths are emitted as POSIX workspace-relative paths and must resolve within the
run workspace. The extractor validates the run status, raw-event hash and count,
single fresh thread, manifest thread identity, and final-message consistency
before publishing output. Recognized credentials and temporal canary values are
replaced with `[REDACTED]` in normalized output; the raw evidence is never
rewritten.

`trajectory_hash` covers the canonical normalized payload but excludes
`extracted_at`, which is explicitly non-deterministic. Event ordering, expanded
file ordering, event IDs, evidence hashes, JSON serialization, and Markdown are
deterministic. A fixed timestamp can be supplied for byte-identical acceptance
tests. Existing output is not overwritten unless `--overwrite` is explicit.

```text
python -m backend.trajectory.extractor .ctm_runs/R-001
python -m backend.trajectory.extractor .ctm_runs/R-001 --fixed-extracted-at 2026-07-16T12:00:00Z --overwrite
```

The output directory contains `trajectory.json`, `trajectory.md`, and
`trajectory_manifest.json`. The Markdown and normalized JSON contain observable
run evidence only: emitted agent messages, commands, command results, declared
file changes, and lifecycle markers. They do not reconstruct hidden
chain-of-thought, infer unstated beliefs or causal explanations, extract general
metrics from arbitrary prose, compare future evidence, or score the run.

## Grounded known-future outcome packets

Phase 4 creates evaluator control-plane context from a verified successful run,
an accepted observable trajectory, and a separately supplied known-future
outcome packet. Scenario placeholder files are not automatically accepted as
ground truth. A packet must explicitly bind to the scenario and base snapshot,
declare that its evidence is `AFTER_CUTOFF`, list independently hashed evidence
files, and define evaluator-only targets.

`CONTROLLED_SYNTHETIC` packets require an unmistakable synthetic fixture notice
and cannot claim organic history. `ORGANIC_HISTORY` packets require non-empty
provenance identifying the historical source and collection method. Evidence
paths are relative to the packet directory and are rejected for traversal,
absolute/drive/UNC forms, symlinks, collisions, missing files, or hash mismatch.

Before that boundary scan, the builder requires the final `workspace/` to be a
real, non-symlink directory whose deterministic tree hash exactly matches the
successful run manifest's `workspace_end_hash`; a missing, added, removed, or
modified final workspace fails closed. Declared evidence paths and identical
file hashes must then be absent from that verified workspace. This proves only
that the declared artifacts were not present as identical files; it cannot
prove semantically equivalent knowledge was impossible. Past trajectory events
and known-future evidence remain separate fields and Markdown sections.

Evaluation output may overwrite only a prior unprotected evaluation directory.
It is rejected before publication if it overlaps the run evidence, workspace,
trajectory output, packet directory, packet file, or declared future evidence.
The bundled LegalRAG packet is a controlled synthetic benchmark fixture with
explicit synthetic metric definitions and is not organic LegalRAG history.

```text
python -m backend.evaluation.context_builder --run-directory .ctm_runs/R-001 --trajectory-directory .ctm_runs/R-001/trajectory --outcome-packet backend/evaluations/legalrag_reranker_t001/outcome.yaml
```

The command emits `evaluation_context.json`, `evaluation_context.md`, and
`evaluation_manifest.json`. `created_at` is excluded from context identity and
can be fixed for byte-identical validation. Phase 4 does not score the agent,
identify a blind spot, generate an intervention or Ghost clue, or perform a
replay. A later phase may compare the separated evidence sets; that comparison
is not implemented here.

## Temporal Blind-Spot Assessment

Phase 5 compares the Phase 4 context's observable past events with its separately
grounded future evidence and evaluator-only targets. Each target receives one of
`SATISFIED`, `PARTIALLY_SATISFIED`, `MISSED`, or `INSUFFICIENT_EVIDENCE` and must
cite existing past event IDs and future evidence IDs under verdict-specific
grounding rules. The assessment records confidence as evaluator uncertainty; it
does not produce a numeric grade, reward, or leaderboard score.

The evaluator receives a compact canonical input rather than raw JSONL, source
code, or machine paths. Evidence text is delimited as untrusted data, external
retrieval and tool calls are forbidden, and a real provider must use one fresh
thread distinct from the Past Codex thread. Model output is validated against a
strict schema and existing evidence identifiers before publication. Synthetic
future evidence remains labelled synthetic and cannot be presented as organic
history or real production performance.

```text
python -m backend.assessment.runner --evaluation-directory .ctm_runs/R-001/evaluation --provider fake
```

Outputs are `evaluator_input.json`, `raw_evaluator_response.txt`,
`blind_spot_assessment.json`, `blind_spot_assessment.md`, and
`assessment_manifest.json`, plus raw evaluator JSONL when supplied by the
provider. This phase evaluates observable actions only; it does not reconstruct
hidden reasoning, generate a replay prompt, create a Ghost clue or intervention,
run a replay, or compare trajectory divergence. A later Ghost phase may consume
the grounded assessment, but that phase is not implemented.

Assessment output is constrained to a child of the accepted run directory; it
cannot publish into a packet, source tree, or any ancestor outside that run.
On any failure after evaluator input construction, the runner preserves a
separate `assessment_failures/<attempt-id>/` bundle containing available input,
raw response, raw JSONL, stderr, and a typed failed-attempt manifest. A failed
retry never replaces an existing accepted assessment. Successful manifests hash
all published evaluator evidence, including stderr.

## Ghost Engineer minimum future clue

Phase 6 deterministically converts a grounded Phase 5 assessment into either a
bounded investigative clue or `NO_INTERVENTION`. The default policy requests
only the missing evaluation dimension: at most two sentences, at most 60 words,
and one investigative action. It does not reveal future results, metric values,
preferred solutions, evaluator verdicts, target/evidence identifiers, or replay
control language. Lexical leakage checks are conservative and do not claim full
semantic non-leakage.

```text
python -m backend.intervention.runner --assessment-directory .ctm_runs/R-001/assessment --generator policy
```

The output contains the evaluator-side `ghost_intervention.json` and Markdown,
plus a separate `replay_intervention.json` limited to schema version,
intervention identity/hash, and the clue. All output remains inside the accepted
run directory and cannot overlap prior phase evidence. This phase generates the
clue only: it does not call a live model, execute replay, compare trajectory
divergence, or implement a GUI.

## Phase 7: controlled counterfactual replay

Phase 7 materializes a new workspace from the same audited sealed snapshot as
the baseline and requires its starting tree hash to equal the baseline starting
hash. It injects only the original task and approved minimum clue, uses a fresh
thread under the same permission-profile boundary, and never copies the
baseline's modified final workspace. Evaluator evidence, assessment rationale,
baseline answers, and future outcome artifacts remain outside the replay.

The runner preserves the exact prompt, raw JSONL, stderr, final response,
isolation artifacts, workspace hashes, and a Phase 3-normalized observable
replay trajectory. Failed attempts are retained separately and cannot replace a
successful replay. The deterministic fake provider is the default. Phase 7 does
not compare trajectories, score improvement or divergence, or reconstruct
hidden reasoning.

Fake replay consumes no Codex credits and uses the explicit execution mode
`DETERMINISTIC_FAKE`, model `deterministic-fake-replay`, and deterministic
reasoning metadata. Routine tests never invoke a live provider. A live replay
requires an explicit `--provider codex`, `--model`, `--reasoning-effort`, and
`--confirm-live-model`; there is no implicit paid-model fallback. GPT-5.6 Sol
High is reserved for one manually approved final acceptance run.
# Phase 7 controlled replay integrity

An accepted intervention manifest records the exact run-relative assessment source
directory that produced it. Replay resolves and verifies that directory; it never
falls back to a directory named `assessment`.

Fake replay providers use a deterministic synthetic thread identifier. A controlled
fake overwrite may reuse that synthetic identifier, while the real Codex provider
must always create a thread distinct from the baseline, evaluator, and every prior
accepted replay. The real provider receives the same validated preflight used by
the replay isolation probe; it does not perform a second preflight.
# Phase 8: Observable History Divergence

Phase 8 compares the accepted baseline and controlled replay trajectories with deterministic, portable event signatures and longest-common-subsequence alignment. It reports matched, baseline-only, replay-only, modified, reordered, expanded, and contracted observable structures; a first observable divergence; neutral behavioral dimensions; and an observable-change outcome.

The comparison consumes only accepted trajectory, run, replay, and intervention-lineage evidence. It does not consume future benchmark values, assessment verdict prose, or intervention rationale. It does not reconstruct hidden chain-of-thought, judge technical correctness, calculate a quality or improvement score, or prove that the clue caused observed changes.

```text
python -m backend.divergence.runner --run-directory <run> --replay-directory <replay> [--output-dir <dir>] [--fixed-created-at <ISO-8601>] [--overwrite]
```

Outputs are `history_divergence.json`, `history_divergence.md`, and `divergence_manifest.json`. Input and output hashes bind the artifact to the exact accepted histories, while protected-path checks prevent publication over earlier-phase evidence.

## Phase 9: Counterfactual target coverage

Phase 9 determines only whether a replay observably addressed each declared
evaluation target. It uses target-specific deterministic policies, successful
ordered command evidence, and accepted Phase 1–8 hashes. Target coverage is
separate from total activity: a replay can contain less overall activity while
still addressing the specific required investigation. It does not establish
technical correctness, retrieval quality, production readiness, causality, or
a preferred solution, and it uses no live model.

```text
python -m backend.counterfactual.runner --run-directory <run> --replay-directory <replay> --divergence-directory <divergence>
```

Outputs are `counterfactual_coverage.json`, `counterfactual_coverage.md`, and
`counterfactual_manifest.json`. The manifest records every available trusted
lineage file with a stable run-relative hash; publication cannot overlap any
resolved prior-phase source directory.

Phase 8 acceptance is additionally anchored by a run-level
`phase_receipts/phase-8.json` receipt outside the replaceable divergence
directory. Phase 9 requires that receipt to match the accepted divergence ID,
canonical hash, and manifest hash. It detects later output edits while the
receipt store remains unchanged; it does not claim protection against an actor
able to rewrite both the evidence and the receipt. Existing accepted Phase 8
evidence can be explicitly receipted with `python -m backend.divergence.receipt`.

## Static hackathon demo

The evidence-driven visual demo is in `frontend/`. It loads the sanitized
`demo-data/R-SMOKE-WSL-001/demo_bundle.json` copied into public assets and
requires no API key, backend service, or live Codex execution. From `frontend/`,
run `npm install`, `npm run dev`, or `npm run validate` for linting, type checks,
tests, and a static production build. The demo renders accepted observable
evidence only and never exposes chain-of-thought.

## Live Demo and Video

- **Live demo:** https://codextimemachine-56sgrk4qu-nitinjaiswalnjwork-5703s-projects.vercel.app/
- **Demo video:** https://youtu.be/hp23r9CMQG8

## How Codex and GPT-5.6 Were Used

Codex was the primary engineering agent used to build Codex Time Machine during OpenAI Build Week. It accelerated repository analysis, architecture design, implementation, test generation, integrity validation, frontend development, and documentation.

GPT-5.6 was used through Codex for the historical controlled acceptance run and for complex implementation work requiring repository-wide reasoning. I made the core product decisions: defining the temporal evidence boundary, limiting Ghost Engineer to a minimum non-answer-revealing clue, separating total activity from target-specific coverage, and requiring every published conclusion to be tied to observable evidence and cryptographic hashes.

Routine regression testing used deterministic fake providers and recorded fixtures to avoid unnecessary live-model calls. The final demo itself is fully static and requires no API key or live model.
