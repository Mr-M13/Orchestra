# Orchestra Testing and Verification Model v1

**Status:** Foundation proposal  
**Version:** 1  
**Date:** 2 October 2026  
**Purpose:** Define what evidence Orchestra may trust, how verification scope and freshness work, how independent verification is represented, and how narrow successful checks avoid being misinterpreted as proof of a broader outcome.

## 1. Core principle

A test result proves only the claim it actually evaluated, in the environment and scope in which it ran.

Orchestra must never silently promote:

```text
"this source-level check passed"
```

into:

```text
"the feature works"
```

or:

```text
"the Goal is achieved"
```

Verification is claim-specific, version-specific, environment-specific and evidence-backed.

## 2. Verification chain

Conceptually:

```text
Claim
  -> Verification Method
  -> Target Version
  -> Environment
  -> Evidence
  -> Verification Result
  -> Higher-level Predicate
  -> Outcome Verification
```

No step may be skipped merely because a lower-level result is green.

## 3. Claim

A **Claim** is a statement Orchestra wants to establish.

Examples:

- this PHP file parses under PHP 8.3;
- this migration preserves existing rows;
- this service behaves correctly under concurrent requests;
- this PR integrates cleanly with current main;
- this login flow works on staging;
- this backup can actually be restored;
- the public outcome criterion is satisfied.

Claims should be specific enough that a PASS has clear meaning.

## 4. Verification

A **Verification** evaluates one or more Claims against Evidence.

It should record:

- claim(s);
- target Artifact Version / external target;
- method;
- verifier;
- environment;
- inputs/fixtures;
- Evidence;
- result;
- exclusions;
- execution time;
- freshness/revalidation conditions.

## 5. Verification result vocabulary

At minimum, verification semantics should distinguish:

- `pass` — evidence supports the claim within scope;
- `fail` — evidence contradicts the claim within scope;
- `inconclusive` — verification ran but cannot establish the claim;
- `not_run` — verification did not occur;
- `blocked` — verification could not run because a required dependency/resource/authority was unavailable;
- `invalid` — the verification itself is not trustworthy for the intended claim;
- `stale` — a historical result exists but no longer satisfies current freshness/context requirements.

A failure to run is not the same as a failed claim.

## 6. Verification scope

Every Verification needs explicit scope.

Common dimensions:

- target identity/version;
- code/data subset;
- environment;
- operating-system/runtime versions;
- dependency versions;
- configuration;
- user role;
- concurrency conditions;
- data fixture;
- network/external-service conditions;
- security/authority context.

A PASS without scope is incomplete evidence.

## 7. Verification levels

These levels describe increasing closeness to real-world outcome. They are not a mandatory universal ladder.

### 7.1 Structural/static verification

Checks structure without executing the full behaviour.

Examples:

- syntax;
- schema shape;
- type checks;
- policy/contract source guards;
- file presence;
- configuration validation.

Useful and cheap, but narrow.

### 7.2 Unit verification

Exercises a bounded component in controlled isolation.

Useful for:

- local logic;
- edge cases;
- failure injection;
- deterministic behaviour.

### 7.3 Component/runtime verification

Executes a real component with relevant runtime dependencies.

Examples:

- PHP + real database;
- service + queue;
- model inference runtime;
- filesystem restore utility.

### 7.4 Integration verification

Exercises interactions between components/systems.

Examples:

- application + database + provider adapter;
- worker result + reconciliation;
- GitHub PR + CI + integration candidate.

### 7.5 End-to-end verification

Exercises a complete user/system path through the intended stack.

Examples:

- public booking from browser through backend persistence;
- backup creation then restore;
- goal-to-task-to-worker-to-artifact orchestration loop.

### 7.6 Environment verification

Confirms behaviour in a particular target environment.

Examples:

- staging;
- production smoke check;
- target device;
- specific cloud region.

### 7.7 Outcome verification

Evaluates whether the actual Goal Outcome Criterion is satisfied.

This may use technical tests, human observation, metrics, external evidence or combinations.

Outcome Verification is not automatically implied by lower-level tests.

## 8. Verification profile

A Task, Artifact, Integration or Outcome Criterion may require a **Verification Profile**.

A profile specifies the classes of evidence required before a predicate can be considered satisfied.

Example:

```text
Task acceptance profile:
  structural source checks
  runtime integration check
  no browser verification required

Goal outcome profile:
  staging end-to-end
  human UX verification
```

The later Policy layer chooses exact profiles and thresholds.

## 9. Evidence provenance

Evidence should identify:

- source;
- producer;
- time;
- target/version;
- method;
- environment;
- immutable external reference where practical.

Examples:

- CI run ID;
- commit SHA;
- screenshot reference;
- test log;
- database snapshot;
- human review record;
- provider result ID.

## 10. Evidence quality

Not all evidence has equal strength.

Possible dimensions include:

- direct vs indirect;
- reproducible vs anecdotal;
- deterministic vs probabilistic;
- independent vs self-reported;
- fresh vs stale;
- production-like vs synthetic;
- complete vs sampled.

The model exposes these dimensions. Policy decides what quality is required.

## 11. Self-reported execution success

A Worker saying:

```text
"I completed the task and tests pass"
```

is an Execution Result, not independent Verification.

It may point to evidence that Orchestra then evaluates.

This distinction is especially important for autonomous agents.

## 12. Independent verification

A Verification is **independent** when the verifier is sufficiently separate from the producer for the required assurance level.

Independence may involve:

- different Worker;
- different tool;
- different process;
- different environment;
- independent human reviewer;
- provider-native check.

Independence is not universally required.

The later Policy layer decides where it matters.

## 13. Verification authority

Verification itself may require specific authority.

Examples:

- human production sign-off;
- security review;
- regulatory approval;
- independent reviewer requirement.

A technically valid test from an unauthorised verifier may still be useful evidence but may not satisfy the required predicate.

## 14. Test target identity

Verification should target immutable or stable identities where possible.

Prefer:

```text
commit SHA abc123
deployment build 91
dataset version v7
model checksum X
```

over:

```text
main
latest
current staging
the new version
```

Moving targets require re-observation.

## 15. Test environment identity

Environment should be identifiable beyond a friendly name.

A verification record may need:

- environment ID;
- deployment/build version;
- relevant configuration digest;
- database/schema version;
- service versions;
- runtime versions.

This prevents passing tests on the wrong environment from satisfying the intended claim.

## 16. Fidelity

Verification fidelity describes how closely the test environment matches the target conditions relevant to the claim.

High fidelity is not always required.

Examples:

- parser test needs little production fidelity;
- migration rehearsal may require real database engine/version;
- browser UX should run against the actual UI stack;
- production cutover confidence may require staging parity.

Policy determines sufficient fidelity.

## 17. Synthetic versus real data

Synthetic data may be preferable for safety and privacy, but it may not reproduce all real-world conditions.

Verification should record data class:

- synthetic;
- fixture;
- anonymised;
- sampled production-like;
- real production;
- generated/adversarial.

A PASS on synthetic data should not imply behaviour against untested real-world edge cases.

## 18. Negative testing

Verification should include evidence that invalid or unsafe behaviour is rejected where relevant.

Examples:

- unauthorised action denied;
- duplicate command does not duplicate side effect;
- malformed input fails safely;
- stale coordinator write rejected;
- missing dependency blocks execution;
- replay conflict detected.

Positive-path tests alone are often insufficient for orchestration guarantees.

## 19. Failure-path testing

Failure/recovery claims require deliberate failure injection.

Examples:

- provider timeout;
- process crash;
- persistence failure;
- credential expiry;
- webhook duplication;
- worker disconnect;
- partial external success.

A recovery system cannot be considered verified solely because the happy path works.

## 20. Concurrency testing

Concurrency claims require actual concurrent execution where concurrency is material.

Source inspection alone cannot prove race safety.

Possible evidence:

- process-level concurrent tests;
- transaction contention;
- lease races;
- coordinator takeover races;
- duplicate delivery/replay.

## 21. Determinism

Verification should record whether results are expected to be deterministic.

For nondeterministic systems, evidence may need:

- repeated trials;
- confidence interval;
- tolerance;
- random seed where relevant;
- distributional acceptance criteria.

A single successful trial may not establish the claim.

## 22. Probabilistic verification

AI/ML systems and some distributed systems may require probabilistic evidence.

The model must support:

- sample size;
- evaluation dataset/version;
- metric;
- threshold;
- uncertainty;
- failure examples.

The Policy layer defines acceptable thresholds.

## 23. Human verification

Some Claims are meaningfully evaluated by humans.

Examples:

- UX quality;
- visual correctness;
- business suitability;
- wording;
- whether an explanation is understandable.

Human verification should still record:

- verifier identity;
- target/version;
- claim;
- evidence/context;
- result;
- scope.

Human judgement should not be disguised as automated certainty.

## 24. Visual verification

Where appearance matters, source-level correctness is not enough.

Useful evidence may include:

- rendered screenshots;
- cross-device views;
- browser interaction;
- visual diff;
- human inspection.

Exact tooling is implementation-specific.

## 25. Security verification

Security-related claims may require:

- negative tests;
- privilege-boundary tests;
- authentication/authority checks;
- secret scanning;
- dependency assessment;
- adversarial tests;
- independent review.

The Security Model determines which authority boundaries exist; the Testing Model determines how claims about them are proven.

## 26. Performance verification

Performance claims should bind to:

- workload;
- environment;
- dataset;
- concurrency;
- metric;
- measurement window.

"Fast" is not a Verification result.

## 27. Reliability verification

Reliability claims may require:

- repeated runs;
- fault injection;
- restart/recovery;
- long-running soak;
- failover;
- data integrity checks.

## 28. Migration verification

Data/schema migrations should consider:

- forward migration;
- preservation of existing data;
- idempotency/replay;
- partial failure;
- rollback/recovery where applicable;
- fresh install;
- upgrade from supported historical states;
- post-migration verification.

## 29. Backup verification

A backup is not verified merely because a file exists.

Useful proof includes:

- integrity check;
- restore rehearsal;
- known-sample validation;
- recovery procedure;
- destination/access validation.

For backup Goals, restore evidence is often closer to outcome truth than backup creation evidence.

## 30. Integration verification

Before accepting integration, Orchestra may need evidence that:

- candidate inputs are the expected versions;
- target base is current;
- conflicts were resolved intentionally;
- no candidate-owned work was silently lost;
- combined tests pass;
- authority boundaries remain intact;
- integration result exists in the authoritative target.

## 31. Staging verification

Staging evidence is useful only when tied to:

- exact deployed version;
- environment identity;
- configuration;
- relevant external services;
- test time.

If staging changes after verification, the result may become stale for current deployment claims.

## 32. Production verification

Production verification should be scoped carefully.

It may include:

- smoke tests;
- health checks;
- read-only validation;
- limited real transactions where authorised.

Production testing must respect Authority and consequence policy.

## 33. Verification freshness

Some evidence remains valid indefinitely; some expires quickly.

Examples:

- immutable source syntax result: durable for that version;
- integration result against moving main: can stale immediately after target moves;
- worker availability: short-lived;
- production health: very short-lived;
- restore rehearsal: may stale after backup process/storage changes.

Freshness should be claim-specific.

## 34. Change impact

When something changes, Orchestra should determine which Verifications remain applicable.

A new change may invalidate:

- only local unit tests;
- integration verification;
- security approval;
- staging UX verification;
- Outcome Verification.

Unchanged claims should not require unnecessary reruns.

## 35. Verification invalidation graph

Future implementation should support relationships like:

```text
Artifact Version changes
  -> source verification stale
  -> integration verification stale
  -> staging deployment no longer same target
  -> outcome verification may stale
```

Invalidation is claim/context based, not blanket "rerun everything".

## 36. Verification reuse

A Verification may be reused if:

- exact target/version remains same;
- relevant environment/context remains equivalent;
- method remains acceptable;
- freshness requirements still hold;
- policy permits reuse.

Reuse should be explicit, not assumed.

## 37. Reproducibility

Where feasible, Verification should preserve enough information to reproduce:

- command/test identifier;
- code/version;
- environment;
- fixture;
- dependency versions;
- relevant configuration.

Not every human review is reproducible, but technical checks usually should be.

## 38. Test flakiness

Flaky tests should be represented as a quality problem.

Repeated reruns until green are not equivalent to a deterministic PASS.

Evidence should preserve:

- failure/pass history;
- rerun count;
- known flake classification;
- root cause status.

Policy may decide whether flaky verification can satisfy a predicate.

## 39. False-positive verification

A test may pass while proving the wrong thing.

Examples:

- testing mocked DB instead of real DB;
- checking source contract when runtime is the concern;
- browser test against stale deployment;
- test assertion accidentally tautological.

Verification method validity must itself be reviewable.

## 40. Verification invalid

Mark Verification `invalid` when its evidence cannot support the intended claim.

Examples:

- wrong target;
- corrupted test;
- test did not actually execute;
- unsupported environment substitution;
- verifier lacked required independence;
- generated fake evidence.

Historical record remains; it simply cannot satisfy the claim.

## 41. Blocked verification

A blocked verification should preserve why it could not run.

Examples:

- no Docker/runtime;
- missing credential;
- provider unavailable;
- test environment absent;
- Human Gate missing.

Blocked != failed claim.

## 42. Verification debt

A project may intentionally progress with some lower-priority Verification pending.

This creates **verification debt**.

Debt should record:

- missing claim/scope;
- reason deferred;
- consequence;
- deadline/trigger if policy defines one;
- what actions are blocked until resolved.

Verification debt must not be hidden behind a generic green state.

## 43. Evidence hierarchy is contextual

There is no universal ranking such as:

```text
unit < integration < human
```

The best evidence depends on the Claim.

For syntax, static analysis may be definitive.

For UX, human/browser evidence may be more relevant.

For concurrency, real concurrent execution matters.

For production state, provider-native observation may be authoritative.

## 44. Verification composition

A higher-level predicate may require multiple Claims.

Example:

```text
"candidate ready for staging"

requires:
  source contract PASS
  runtime migration PASS
  security predicate PASS
  integration candidate current
```

One PASS does not override another required FAIL or NOT RUN.

## 45. Contradictory evidence

If evidence conflicts:

- preserve all material evidence;
- prefer evidence authoritative for the specific Claim;
- inspect target/version/environment mismatch;
- do not average contradictions into certainty;
- mark Verification inconclusive if necessary;
- create investigation/reconciliation Task.

## 46. Independent review

Review is a kind of Verification when it evaluates explicit Claims.

A review should record:

- reviewer;
- target/version;
- findings;
- scope;
- result;
- unresolved findings.

"Reviewed" alone is too vague.

## 47. Findings

Verification may produce Findings with severity and status.

A Finding can be:

- blocking;
- non-blocking;
- informational.

The later Policy layer decides which classes block which transitions.

## 48. Finding lifecycle

Conceptually:

```text
open
  -> corrected
  -> accepted
  -> waived
  -> invalid
  -> superseded
```

Waiver requires appropriate Decision/Authority.

Corrected findings should be reverified where needed.

## 49. Test oracle

A Verification needs an **oracle**: the rule by which success/failure is decided.

Examples:

- expected output;
- invariant;
- contract;
- threshold;
- human judgement criteria;
- external authoritative state.

If the oracle is wrong, the test can be green and useless.

## 50. Contract verification

A contract test should identify the contract source/version.

This prevents stale tests from appearing authoritative after the contract evolves.

## 51. Test ownership

Tests themselves need ownership and maintenance.

Useful metadata may include:

- owning project/module;
- claim covered;
- last validated;
- expected environment;
- known limitations.

This helps identify stale verification guards.

## 52. Verification of verification

Critical test infrastructure may itself require verification.

Examples:

- ensure test actually fails when defect injected;
- mutation testing;
- known-failing fixture;
- negative control;
- audit that runtime harness matches declared schema/version.

This is especially valuable after experiencing stale or tautological guards.

## 53. Outcome Verification

Outcome Verification evaluates the Goal's Outcome Criteria.

It should ask:

- what exact criterion is being evaluated?
- what current evidence supports it?
- is that evidence fresh enough?
- does it cover the intended population/environment?
- are relevant exceptions/waivers authorised?
- what remains unknown?

A Goal becomes satisfied only when required Outcome Criteria are currently satisfied or validly waived.

## 54. Outcome versus proxy

A proxy metric may support but not replace the true criterion unless explicitly designed that way.

Example:

```text
Proxy:
  all tests green

Outcome:
  real user can successfully complete intended workflow
```

If the Goal is user success, tests are evidence, not the Goal itself.

## 55. Partial outcome verification

A Goal may have multiple criteria.

Some may pass while others remain pending.

Orchestra should expose that partial truth instead of one binary "green/red" projection.

## 56. Continuous outcome verification

Some Goals require ongoing validity rather than one-time proof.

Examples:

- service uptime;
- backup reliability;
- security posture;
- data freshness.

The model must support recurring Outcome Verification without making recurrence universal.

## 57. Verification triggers

Possible triggers include:

- artifact change;
- integration;
- deployment;
- environment change;
- dependency/version change;
- policy change;
- authority change;
- scheduled freshness expiry;
- manual request;
- incident/recovery.

Exact trigger rules belong to Policy.

## 58. Verification cost

Verification has cost:

- compute;
- provider quota;
- money;
- time;
- human attention.

Planning should choose sufficient evidence, not maximal evidence by default.

The Policy layer balances assurance against cost/risk.

## 59. Risk-based verification

Higher consequence may justify stronger verification.

The model should support risk-sensitive profiles without defining universal risk thresholds.

## 60. Fast feedback versus deep proof

Useful workflow often layers:

```text
cheap fast checks
  -> targeted runtime
    -> integration
      -> environment verification
        -> outcome verification
```

Fail early where cheap, but do not stop the proof chain prematurely when stronger evidence is required.

## 61. Verification scheduling

Independent verification may run:

- before integration;
- after integration;
- before deployment;
- after deployment;
- periodically;
- after recovery.

The model permits these; Policy chooses them.

## 62. Verification worker selection

A verifier may require:

- particular Capability;
- independence;
- environment access;
- authority;
- trust level.

The scheduler should treat verification as real work, not a passive status flag.

## 63. Evidence retention

Evidence should be retained long enough to support:

- audit;
- recovery;
- reproducibility;
- decisions;
- verification freshness reasoning.

Large logs/artifacts can remain external with stable references where appropriate.

## 64. Privacy and security

Testing must respect:

- secret minimisation;
- sensitive data handling;
- least privilege;
- production authority;
- audit privacy.

Verification does not justify copying production secrets or personal data unnecessarily.

## 65. Untrusted test content

Tests or fixtures may contain untrusted content.

They must not silently modify Orchestra Instructions or Authority.

This follows the Security Model's data-versus-instruction boundary.

## 66. Verification after recovery

Recovery may invalidate prior verification.

Examples:

- Worker changes code;
- base changes;
- environment is rebuilt;
- credentials/authority context changes;
- partial artifact is salvaged and adapted.

Recovery should identify which Claims require re-verification.

## 67. Verification after supersession

Superseded artifacts retain historical Verification.

The successor needs its own applicable Verification unless evidence can be validly reused.

## 68. Verification after manual external change

Manual drift may stale:

- environment verification;
- integration verification;
- security approval;
- outcome evidence.

Reconciliation should identify affected Claims.

## 69. Policy hooks exposed by this model

The later Policy layer should configure:

- required Verification Profiles;
- required independence;
- freshness windows;
- blocking Finding severity;
- acceptable environment fidelity;
- test rerun strategy;
- flaky-test handling;
- probabilistic thresholds;
- production verification limits;
- human review requirements;
- risk-based assurance;
- verification debt tolerance.

This document does not choose those values.

## 70. Scenario tests

### 70.1 Source tests pass, staging broken

```text
Verification A
  source contracts
  PASS

Verification B
  staging browser flow
  FAIL

Result
  source claim remains PASS
  staging outcome not satisfied
  no global "verified" state
```

### 70.2 Runtime environment unavailable

```text
Verification
  WordPress/MariaDB runtime
  BLOCKED

Source checks
  PASS

Result
  source-level predicates may pass
  runtime-dependent predicates remain unsatisfied
```

### 70.3 Main changes after PR verification

```text
PR commit X verified
main moves

Source verification of X
  still historically PASS

Integration claim against current main
  stale

Recovery
  refresh integration target
  rerun only affected verification
```

### 70.4 Human visually approves old build

```text
Build 41
  visual review PASS

Build 42 deployed

Result
  review of 41 remains historical
  cannot satisfy claim about build 42
```

### 70.5 Test passes because guard is stale

```text
Contract evolved
test still asserts old contract

Verification
  may PASS mechanically

Audit
  oracle/contract mismatch discovered

Result
  verification marked INVALID for current claim
  test requires maintenance
```

### 70.6 Backup exists but restore fails

```text
Backup creation
  PASS

Restore rehearsal
  FAIL

Outcome Criterion:
  recoverable backup
  NOT SATISFIED
```

### 70.7 Agent says tests pass but no evidence exists

```text
Execution Result:
  "tests pass"

Evidence:
  absent

Result:
  no Verification recorded
  acceptance predicate remains unsatisfied
```

### 70.8 Independent reviewer finds semantic conflict

```text
Candidate unit/runtime tests
  PASS

Independent integration review
  FAIL

Result
  candidate remains valid in narrower scope
  integration predicate fails
  correction Task created
```

## 71. Verification anti-patterns

Orchestra should avoid:

- one global `verified=true`;
- PASS without target version;
- PASS without environment/scope;
- worker self-report treated as proof;
- blocked test reported as PASS;
- NOT RUN reported as FAIL;
- source tests used as runtime evidence;
- green CI used as goal satisfaction;
- repeated reruns until a flaky test passes;
- visual claims verified only by source inspection;
- concurrency claims verified only by static review;
- backup existence treated as recoverability;
- old environment evidence reused after material drift;
- stale contract tests treated as authoritative;
- screenshots without target/build identity;
- fake/mocked environment presented as real runtime proof.

## 72. Minimal implementation obligations

Before Orchestra can safely automate meaningful acceptance decisions, it should support durable representation of:

- Claim;
- Verification;
- Verification Scope/Profile;
- target/version;
- environment identity;
- Evidence references;
- result vocabulary;
- verifier identity;
- freshness/invalidation;
- Findings;
- Outcome Verification.

The exact storage model remains an ADR decision.

## 73. Relationship to the Policy layer

The Testing Model defines the vocabulary and semantics.

Policy will later answer questions such as:

- Does this Task need independent review?
- How recent must staging verification be?
- Is source-only proof enough?
- Which findings block merge?
- How many probabilistic trials are enough?
- When is human visual verification mandatory?

Those are intentionally not foundation rules.

## 74. Relationship to Learning Mode

Learning Mode can explain:

- why a particular test proves only a narrow claim;
- why runtime proof is still missing;
- why a previous PASS became stale;
- why independent verification is required;
- what a failure actually means;
- how the evidence chain connects to the Goal.

This makes verification understandable rather than merely procedural.

## 75. Next foundation step

The next document should define **Learning Mode v1**.

With Goal, Task, Decision, Evidence, Verification, Failure, Recovery and Authority semantics now established, Learning Mode can attach explanations to real work without becoming a parallel source of truth.

After Learning Mode, Orchestra should be ready to begin Architecture Decision Records for persistence/state architecture, execution model and the first implementation slice.
