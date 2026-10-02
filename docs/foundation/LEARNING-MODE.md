# Orchestra Learning Mode v1

**Status:** Foundation proposal  
**Version:** 1  
**Date:** 2 October 2026  
**Purpose:** Define how Orchestra can increase human understanding through real project work without turning teaching into a second control plane, fabricating a model of the person, exposing sensitive information, or unnecessarily slowing execution.

## 1. Core principle

Learning Mode should make the human more capable **while the real work continues**.

It is not a separate course layered on top of Orchestra.

The central relationship is:

```text
real Goal
  -> real Decision / Task / Failure / Verification
    -> useful explanation at the moment it matters
      -> stronger human understanding
```

Learning should remain connected to the actual project context.

## 2. Two outputs

Orchestra may produce two kinds of value from the same work:

```text
PROJECT OUTPUT                 HUMAN CAPABILITY

working system                 understanding
verified artifact              judgement
reliable process               operational knowledge
decision record                decision-making context
recovery path                  troubleshooting ability
documentation                  greater independence
```

The project output remains primary unless the human explicitly chooses a learning-heavy interaction.

## 3. Learning is not authority

An Explanation:

- does not approve a Decision;
- does not satisfy a Human Gate;
- does not change a Goal;
- does not alter a Plan;
- does not grant Authority;
- does not establish Verification;
- does not replace Evidence.

Learning Mode interprets authoritative project state. It does not become authoritative project state.

## 4. Learning is not orchestration

The orchestration system should not create a second workflow merely to teach every action.

Teaching may attach to:

- Goal;
- Plan Revision;
- Task;
- Decision;
- Human Gate;
- Artifact;
- Verification;
- Failure;
- Recovery Action;
- Integration;
- Outcome Verification.

The underlying object remains the source of project meaning.

## 5. Explanation

An **Explanation** is a learning-oriented interpretation attached to real project work.

An Explanation should be able to answer some combination of:

- What is this?
- Why does it matter here?
- What problem does it solve?
- What are the realistic alternatives?
- Why did Orchestra choose or propose this route?
- What evidence supports it?
- What remains uncertain?
- What could go wrong?
- How would I operate or troubleshoot it?
- What should I remember for next time?

Not every Explanation needs every answer.

## 6. Explanation provenance

Where an Explanation makes factual claims about project state, it should derive them from the same authoritative records, observations and Evidence used by Orchestra.

If Orchestra says:

> The runtime test is still missing.

that statement should be traceable to Verification state.

If it says:

> We chose option B because it preserves rollback.

that should be traceable to the relevant Decision/Plan rationale.

Learning Mode should not invent a cleaner story than the project history supports.

## 7. Explanation versus rationale

A **Decision rationale** records why a Decision was made.

An **Explanation** helps a human understand that rationale.

The rationale is authoritative historical context.

The Explanation may be:

- shorter;
- more detailed;
- simplified;
- analogy-based;
- adapted to the human's current needs.

Changing the Explanation must not rewrite the Decision rationale.

## 8. Interaction depth

Learning Mode should support at least three conceptual depths.

### 8.1 Just do it

Optimise for progress.

Still surface:

- genuine human Decisions;
- material uncertainty;
- significant risk;
- important failures;
- irreversible or consequential actions;
- material changes to the Goal/Plan.

Routine technical detail stays quiet.

### 8.2 Explain important decisions

Continue work while explaining:

- meaningful architecture choices;
- important trade-offs;
- why a recovery route was selected;
- what significant Verification proves;
- why a Human Gate exists;
- what materially changed.

This should be a practical default for a human who wants understanding without a tutorial for every command.

### 8.3 Teach me this task

Use the current real task as a structured learning opportunity.

May include:

- prerequisite concepts;
- step-by-step reasoning at an appropriate level;
- alternatives;
- implementation mechanics;
- troubleshooting;
- checkpoints;
- optional questions or teach-back.

Execution should still continue unless the human asks to participate directly.

## 9. Depth is not global verbosity

A learning level should not mean every message becomes long.

Even in `Teach me this task`, Orchestra should explain what is relevant to the current work rather than dumping generic background.

Likewise, `Just do it` should not suppress consequential information.

## 10. Local overrides

The human should be able to change explanation depth:

- globally;
- for a Project;
- for a Goal;
- for a Task;
- temporarily for a concept.

Examples:

```text
"Just handle Git for me."
"Teach me the database part."
"Explain why this failed."
"Skip the implementation detail."
"Go deeper on the architecture."
```

A local preference should not require redesigning the underlying Task.

## 11. Progressive disclosure

Explanations should generally start at the minimum useful depth and allow expansion.

Conceptually:

```text
What happened?
  -> Why?
    -> How does it work?
      -> Alternatives/trade-offs
        -> implementation detail
```

This avoids overwhelming the human while keeping depth available.

## 12. Explain at the decision point

The most valuable moment to explain a concept is often when it affects a real Decision.

For example:

```text
Decision:
  SQLite or PostgreSQL for first implementation

Useful learning:
  what persistence requirements matter here
  what each option changes
  what is reversible
  what future constraints each introduces
```

This is more useful than an unrelated database lecture.

## 13. Explain after evidence changes

Learning Mode should highlight when new Evidence changes the interpretation.

Example:

```text
Earlier:
  source tests passed

Now:
  staging runtime fails

Explanation:
  source correctness was real but narrower than runtime correctness
```

This teaches the model of evidence using actual project experience.

## 14. Explain failures without flattening them

A Failure explanation should distinguish:

- what failed;
- what did not fail;
- what remains valid;
- what is unknown;
- what recovery is being attempted;
- why that recovery is appropriate.

This avoids teaching the false lesson that every failed Attempt means the whole route was bad.

## 15. Explain recovery

Recovery is especially valuable for learning because it reveals system boundaries.

A useful recovery explanation may show:

- original assumption;
- observed failure;
- preserved work;
- changed context;
- retry/reassign/reconcile/replan choice;
- evidence required to resume confidence.

## 16. Explain Verification scope

Learning Mode should translate Verification into human meaning.

Example:

```text
Technical:
  source contract PASS
  runtime NOT RUN

Explanation:
  We know the code satisfies the source-level contract.
  We do not yet know whether it works inside the real runtime.
```

It should not merely repeat status labels.

## 17. Explain uncertainty

Orchestra should distinguish:

- known;
- strongly evidenced;
- assumed;
- uncertain;
- unknown;
- disputed/contradictory;
- stale.

Learning Mode should make those distinctions understandable rather than smoothing them into confident prose.

## 18. Explain alternatives honestly

When multiple routes are reasonable, Learning Mode should present the real trade-offs.

It should avoid pretending the selected route was inevitable.

A useful explanation may cover:

- why alternatives were considered;
- why one currently fits better;
- what would make another option preferable;
- how reversible the choice is.

## 19. Technical decisions versus human decisions

Learning Mode should help the human recognise three broad classes:

### 19.1 Routine technical choice

Can usually be delegated under Policy.

Explanation is optional unless useful.

### 19.2 Uncertain engineering choice

May require investigation, measurement or trade-off reasoning.

Explanation should expose uncertainty and evidence.

### 19.3 Genuine human decision

Depends on goals, values, cost, risk or direction.

Orchestra should explain enough for informed human choice but must not turn explanation into pressure or hidden recommendation beyond the evidence.

## 20. Decision literacy

Over time, Learning Mode should help the human understand:

- which choices are consequential;
- which are reversible;
- which require evidence;
- which are mostly implementation detail;
- what questions are worth asking.

The objective is better judgement, not memorisation of terminology.

## 21. Operational learning

A finished system is more useful when the human understands how to operate it.

Learning may include:

- where to check status;
- what normal looks like;
- common failure signals;
- safe recovery steps;
- what should trigger escalation;
- where authoritative state lives;
- how to verify success.

This should derive from the actual implemented system.

## 22. Maintenance learning

Orchestra should explain enough that the human is not unnecessarily dependent on Orchestra for routine maintenance.

Possible topics:

- upgrade path;
- backup/restore;
- credential renewal;
- test interpretation;
- deployment flow;
- service restart;
- log location;
- rollback.

The depth depends on the human's preference and responsibility.

## 23. Knowledge Profile

A **Knowledge Profile** is an optional private model of what the human appears to understand, wants to learn, or prefers not to have repeatedly explained.

It exists only to improve explanation relevance.

It must not be treated as authoritative truth about the person.

## 24. Knowledge Profile uncertainty

Knowledge observations may be:

- explicitly stated;
- demonstrated during work;
- inferred tentatively;
- outdated.

The profile should preserve that distinction.

Example:

```text
Topic: Git rebase

explicit preference:
  user does not want routine command-level explanation

Topic: orchestration state models

observed:
  user understands core principle
  detailed vocabulary may still be unfamiliar
```

An inference should not become a permanent label.

## 25. No competence scoring

Learning Mode should not reduce a person to a single:

- beginner/intermediate/expert score;
- intelligence score;
- competence ranking.

Knowledge is domain-specific.

A person can be highly experienced in one area and new to another.

## 26. No hidden gatekeeping

The Knowledge Profile must not decide:

- whether the human may own a Goal;
- whether their Decision counts;
- whether they receive Authority;
- whether they are allowed to attempt something.

Security/Authority policy controls those questions, not inferred knowledge.

## 27. Human corrections

The human should be able to correct the learning model naturally.

Examples:

```text
"I know Git; skip that."
"I've never used Docker."
"Explain networking more simply."
"Don't teach me this part."
```

Corrections should take precedence over weak inference.

## 28. Forgetting and staleness

Knowledge Profile entries can become stale.

Orchestra should not assume that something explained once is permanently understood.

Likewise, it should not repeatedly explain a concept merely because the user once asked about it.

The future implementation may use recency/confidence, but exact mechanics are deferred.

## 29. Learning preference versus knowledge

These are different.

A human may understand a subject but still want detailed explanation.

A human may be new to a subject but prefer Orchestra to just do it.

Therefore:

```text
knowledge estimate != desired explanation depth
```

## 30. Questions

Learning Mode may ask questions when they materially improve learning or are required for a Decision.

It should not constantly quiz the human.

Questions may be useful to:

- reveal a misconception;
- help the human choose between trade-offs;
- check whether to go deeper;
- invite prediction before showing evidence;
- support teach-back when requested.

## 31. Teach-back

Optional teach-back can strengthen understanding.

Example:

> Before we deploy, want to tell me what you expect the staging verification to prove?

Teach-back should be opt-in or naturally appropriate, not an obstacle to progress.

A wrong answer must not affect human Authority.

## 32. Human participation

In `Teach me this task`, the human may choose to perform part of the work.

Examples:

- write a small query;
- inspect a diff;
- interpret a test;
- run a safe command;
- choose an architecture option.

Participation is optional.

Learning Mode should never manufacture manual work merely to make the human "earn" understanding.

## 33. Safe practice environments

When hands-on learning is desired, Orchestra should prefer:

- disposable environment;
- sandbox;
- branch/worktree;
- synthetic data;
- staging;
- reversible operation.

Learning is not justification for unnecessary production risk.

## 34. Explanation timing

Explanations may be:

- before action — when understanding affects consent/Decision;
- during action — when a concept becomes relevant;
- after action — to interpret result/evidence;
- on demand — when the human asks;
- at milestone — to consolidate what changed.

The mode should avoid interrupting every low-level step.

## 35. Attention budget

Human attention is a limited Resource.

Learning Mode should spend it where it creates durable value.

Prefer explaining:

- unfamiliar consequential concepts;
- recurring concepts;
- architectural decisions;
- failures/recovery;
- operational knowledge;
- surprising evidence;
- irreversible choices.

De-emphasise:

- repetitive command syntax;
- transient implementation noise;
- details easily retrieved later unless requested.

## 36. Learning debt

A project may intentionally defer explanation to preserve momentum.

This creates **learning debt** only when the missing understanding is likely to matter later for:

- operation;
- maintenance;
- safety;
- future Decisions;
- independence.

Learning debt is not every unexplained technical detail.

## 37. Milestone recap

At useful milestones, Orchestra may summarise:

- what was accomplished;
- what changed in the system;
- important decisions;
- what evidence now exists;
- what remains uncertain;
- key concepts learned;
- what the human could now operate or recognise.

This connects progress with capability growth.

## 38. Concept map

Learning Mode may maintain relationships between concepts.

Example:

```text
Task
  -> Attempt
    -> Result
      -> Evidence
        -> Verification
          -> Outcome Criterion
```

When a new concept appears, Orchestra can connect it to concepts already understood.

## 39. Project-specific vocabulary

Learning should teach the vocabulary actually used by the Project.

For Orchestra itself, examples include:

- Goal;
- Outcome Criterion;
- Attempt;
- Evidence;
- Verification;
- Human Gate;
- Reconciliation;
- Supersession.

For another Project, the useful vocabulary may be entirely different.

## 40. Avoid jargon as status

Technical terminology should improve precision, not establish hierarchy.

When introducing a term, Orchestra should explain what practical distinction it preserves.

Example:

> We use "Attempt" separately from "Task" so one failed run doesn't make the intended work itself failed.

## 41. Analogies

Analogies can help but must preserve the important distinction.

Orchestra may use an analogy, then state where it stops being accurate if that matters.

The orchestra/conductor metaphor itself is an example: useful for coordination, but not a literal execution architecture.

## 42. Explanation correctness

An Explanation can itself be wrong or become stale.

Important explanations should retain enough linkage to:

- source Decision;
- Verification;
- Artifact Version;
- Plan Revision;
- Evidence;

so that Orchestra can regenerate or correct them when project truth changes.

## 43. Correcting explanations

If an earlier Explanation was misleading:

- preserve the underlying project history;
- correct the Explanation;
- identify what changed or what was previously misunderstood;
- avoid pretending the earlier explanation never happened when the correction matters.

## 44. Learning from incidents

Incidents and failures can produce durable lessons.

A post-incident learning summary may explain:

- expected behaviour;
- actual behaviour;
- failed assumption;
- evidence;
- recovery;
- system change;
- operational lesson.

This can become Project Knowledge if verified and useful.

## 45. Learning from repeated patterns

If the same concept repeatedly appears, Orchestra may consolidate it.

Example:

After several stale-branch recoveries, the human may benefit more from understanding:

> Why moving integration targets invalidate some evidence

than from receiving another isolated Git explanation.

## 46. Learning from Decisions

Decision history can show how judgement evolves.

Learning Mode may compare:

- what was known then;
- what option was chosen;
- what happened;
- what new evidence emerged;
- whether the rationale still holds.

This supports better future judgement without rewriting history.

## 47. Learning from superseded work

Superseded work can still teach useful lessons.

The explanation should distinguish:

- "this was wrong";
- "this was valid then but no longer current";
- "this was replaced by a better route";
- "the Goal changed".

This reinforces the State Model's historical semantics.

## 48. Learning from Verification

Verification provides a natural teaching structure:

```text
Claim
  -> how we tested it
  -> what evidence appeared
  -> what that proves
  -> what it does not prove
```

This is preferable to teaching testing as abstract ceremony.

## 49. Learning from security boundaries

Security explanations should focus on principles without exposing secrets.

Useful examples:

- why Access differs from Authority;
- why staging permission does not imply production permission;
- why approval is bound to an exact version;
- why credentials should not be pasted into prompts.

Do not reveal protected values to make the lesson concrete.

## 50. Privacy

Knowledge Profiles and learning preferences may reveal personal information about the human.

They should be treated as private Project/user context.

Future implementation should support:

- visibility;
- correction;
- minimisation;
- deletion/retention policy;
- separation from public project artifacts.

Exact privacy architecture is deferred.

## 51. Cross-project learning

Some knowledge may transfer across Projects.

Examples:

- Git concepts;
- database fundamentals;
- verification principles.

But project-sensitive knowledge should not automatically leak across Project boundaries.

Cross-project learning should respect privacy and security policy.

## 52. Sensitive inference

Learning Mode should avoid unnecessary inference about personal traits.

It does not need to infer:

- intelligence;
- education level;
- personality diagnosis;
- protected characteristics.

It needs only enough context to make explanations useful.

## 53. Learning and accessibility

Explanations should be adaptable in presentation:

- concise vs detailed;
- terminology-heavy vs plain language;
- diagram/analogy vs prose;
- step-by-step vs conceptual;
- language/localisation where supported.

Presentation preference is not a competence judgement.

## 54. Learning and uncertainty

If Orchestra itself is uncertain, it should teach the uncertainty.

Example:

> There are two plausible causes. The current evidence does not distinguish them, so we're testing X next because that result will separate the hypotheses.

That is more educational and more truthful than pretending to know the answer.

## 55. Learning and research

When a Decision requires external research, Learning Mode can expose:

- what question is being researched;
- which sources are authoritative;
- what evidence conflicts;
- what remains uncertain;
- how the research changes the Plan.

It should distinguish source facts from Orchestra interpretation.

## 56. Learning and automation

Automation should not make the system opaque.

For important automated behaviour, Orchestra should be able to explain:

- trigger;
- decision basis;
- authority;
- action;
- evidence;
- recovery path.

The human need not watch every automation run.

## 57. Learning and policy

When Policy later controls behaviour, Learning Mode should be able to answer:

> Why did Orchestra do that?

with a chain such as:

```text
Failure observed
  -> policy classified retry as safe
    -> authority allowed autonomous retry
      -> Recovery Action created
        -> new Attempt started
```

This is explanation of policy application, not the policy itself.

## 58. No retrospective fiction

Orchestra must not explain a result as though it had planned or predicted something it only learned afterward.

It should distinguish:

- prior rationale;
- later evidence;
- retrospective lesson.

This is essential for trustworthy learning.

## 59. Confidence language

Explanation language should match evidence.

Prefer:

- "we verified";
- "the current evidence suggests";
- "we assumed";
- "we have not tested";
- "this is one plausible explanation";

according to actual state.

Avoid unjustified certainty.

## 60. Explanation artifacts

Some explanations may deserve durable Artifact form:

- runbook;
- architecture guide;
- decision explainer;
- troubleshooting guide;
- onboarding document.

Not every conversational explanation should become an Artifact.

The Plan/Task may create durable learning artifacts when useful.

## 61. Generated documentation freshness

Learning-oriented documentation can stale as the system changes.

Durable guides should identify:

- relevant system/version;
- source Decisions/Artifacts where practical;
- last verification/update;
- known limitations.

Documentation is not automatically current merely because it exists.

## 62. Learning outcome

A **Learning Outcome** is an optional statement of what capability the human wanted to gain.

Examples:

- understand enough Git to review integration history;
- operate the backup restore process;
- understand why staging differs from source tests.

Learning Outcomes are optional and human-owned.

They are not required for every Goal.

## 63. Learning Outcome verification

If the human explicitly wants to verify learning, evidence might include:

- successful guided operation;
- explanation in their own words;
- correct troubleshooting;
- independent completion of a safe task.

This should only be used when the human wants it.

Orchestra should not grade the human by default.

## 64. Knowledge evidence

A Knowledge Profile may use weak evidence such as:

- human explicitly says they know a topic;
- human asks for deeper detail;
- human successfully performs a related task;
- human repeatedly skips explanations.

These are adaptation signals, not formal competency certification.

## 65. No hidden scoring

Any future adaptation mechanism should avoid opaque scores that materially affect the human experience without explanation.

If Orchestra suppresses or expands explanation because of a preference/profile signal, the human should be able to override it immediately.

## 66. Learning failure

Learning Mode itself can fail.

Examples:

- explanation too abstract;
- too verbose;
- assumes missing prerequisite;
- analogy misleads;
- repeats known material;
- omits a critical risk;
- interrupts execution too often.

The recovery is adaptation, not judgement of the human.

## 67. Feedback loop

Useful feedback may be lightweight:

- "too much detail";
- "go deeper";
- "I know this";
- "explain that term";
- "show me how this connects";
- "just do it".

These signals can refine future explanation without becoming project authority.

## 68. Scenario tests

### 68.1 Human wants progress, not Git lessons

```text
Mode:
  Explain important decisions

Task:
  routine branch update

Result:
  Orchestra performs it quietly

Later:
  merge conflict changes integration semantics

Result:
  Orchestra explains the conflict and decision
```

Learning follows significance, not every command.

### 68.2 Human asks why a green test was insufficient

```text
Verification:
  source PASS
  runtime NOT RUN

Explanation:
  source claim is established
  runtime behaviour remains unknown

Project state:
  unchanged by the Explanation
```

### 68.3 User understands concept after repeated real examples

```text
Knowledge Profile:
  "Task vs Attempt" appears understood
  evidence: repeated correct use

Future:
  explanations become shorter

Human says:
  "Explain that again"

Result:
  expand immediately
```

No permanent label blocks explanation.

### 68.4 Human wants to learn deployment

```text
Mode:
  Teach me this task

Orchestra:
  explains staging/production distinction
  uses staging
  shows verification
  allows optional safe participation

Production:
  remains governed by normal Authority/Policy
```

Learning does not weaken safety.

### 68.5 Earlier explanation becomes wrong

```text
Earlier:
  failure attributed to provider outage

New Evidence:
  local credential expiry

Result:
  correct explanation
  preserve original evidence/history
  state why interpretation changed
```

No retrospective fiction.

### 68.6 AI-generated explanation contains unsupported certainty

```text
Explanation:
  "The deployment is definitely safe"

Evidence:
  source PASS
  staging NOT RUN

Result:
  explanation invalid/corrected
  project Verification remains unchanged
```

### 68.7 Human chooses Just do it

```text
Routine reversible work:
  minimal explanation

Human Gate:
  production DNS cutover

Result:
  still surface decision, evidence, risk and consequence
```

Low explanation does not remove informed human control.

## 69. Learning anti-patterns

Orchestra should avoid:

- teaching every command;
- generic tutorials detached from real work;
- assuming "beginner" globally;
- competence scoring from a few questions;
- using knowledge inference to restrict human authority;
- quizzes that block progress;
- forcing manual work for educational theatre;
- hiding uncertainty to make explanations simpler;
- rewriting history into a cleaner story;
- explanations that overstate Verification;
- exposing secrets for demonstration;
- repeatedly teaching concepts the human has asked to skip;
- suppressing explanation because a stale profile says "expert";
- making Learning Mode a separate orchestration state machine;
- treating explanation as Evidence;
- optimising for course completion instead of the human's Goal.

## 70. Minimal implementation obligations

A first Learning Mode implementation likely needs:

- explanation-depth preference;
- per-Task/on-demand override;
- Explanation linked to project objects;
- source/provenance references;
- simple private learning preferences;
- optional Knowledge Profile observations;
- correction/override mechanism;
- security/redaction boundary.

It does not require a sophisticated tutoring engine.

## 71. What this model intentionally does not decide

Learning Mode v1 does not choose:

- UI;
- LLM/provider;
- memory implementation;
- embedding/vector store;
- Knowledge Profile schema;
- scoring algorithm;
- educational pedagogy framework;
- quiz system;
- spaced repetition;
- exact explanation length;
- automatic teaching frequency;
- whether a Knowledge Profile is enabled by default;
- cross-project memory implementation;
- retention period.

Those choices should follow from user value, privacy and implementation evidence.

## 72. Foundation completion criteria

With Learning Mode v1, the foundation now defines:

- what Orchestra is trying to achieve;
- the domain concepts it reasons about;
- where authoritative truth lives;
- how failure and recovery work;
- how security and authority are separated;
- what evidence and verification mean;
- how human understanding can grow alongside execution.

The next step should no longer be another broad conceptual model by default.

It should be to turn these constraints into explicit **Architecture Decision Records** and choose the smallest implementation architecture that can prove the foundation in practice.

## 73. Recommended ADR sequence

The first ADRs should address, in order:

1. **Authoritative persistence model** — how Goals, Plans, Tasks, Attempts, Decisions, Evidence and history are durably represented.
2. **Execution/coordinator model** — how work is scheduled, authority epochs work and reconciliation occurs.
3. **Adapter boundary** — how GitHub, workers, tools and future providers connect without becoming Orchestra's identity.
4. **Policy model** — how configurable operational rules are represented separately from foundation semantics.
5. **First vertical slice** — the smallest end-to-end implementation that exercises Goal -> Task -> Attempt -> Result -> Verification -> Outcome without overbuilding.

The ADRs should be allowed to reject attractive technologies when they conflict with the foundation.

## 74. Final foundation test

Before implementation, Orchestra should be able to explain its own design in one sentence:

> Orchestra coordinates the right capabilities toward a human-owned Goal, preserves authoritative state and evidence, recovers safely from failure, respects bounded authority, verifies outcomes without false certainty, and helps the human understand the important parts along the way.

If implementation choices weaken that sentence, the architecture should be reconsidered.
