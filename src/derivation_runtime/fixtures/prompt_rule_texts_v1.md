# Prompt rule texts v1 (test fixture)

This file is DerivationLab's frozen prompt contract: it fixes the exact English
wording of the prompt paragraphs that the Writer and Checker developer texts
carry. The tests `test_intent_ledger_first.py`, `test_step_slot_references.py`,
`test_dimension_check.py` and `test_fork_control.py` compare the constants in
`prompts.py` against these blocks, so the code cannot drift from the contract.
Only line wrapping may differ between a block and the code. Changing a prompt
paragraph means changing its block here in the same commit.

Headings are plain labels. The tests locate sections by them, so keep them
unchanged.

## Intent ledger first (switchable obligation)

Configuration: the per-run boolean `intent_ledger_first` (default false).

### Writer paragraph (only when the flag is on, directly after the dependency-ledger obligation)

```
Before any derivation step, the first step of every route must be an intent ledger. It states: (i) the starting object and the quantity you will compute; (ii) the endpoint form you will deliver; (iii) every approximation, standard result and identity you intend to rely on, each with status HYPOTHESIS and, where you already know it, the allowlisted source span you expect to support it; (iv) the class of Hamiltonian and operators you intend to assume. The intent ledger contains no derivation. Later steps may add, drop or replace entries; the final dependency ledger must reconcile with it: every intent entry is either carried forward with its final status or explicitly retired with a reason.
```

### The Checker's paragraph (only when the flag is on)

```
When the target step is the first step of its route and the run declares intent_ledger_first, check only two things: that every cited source span exists and supports the intended use, and that the entries are mutually consistent (for example, the declared Hamiltonian class admits the operators the candidate intends to use). Do not judge the completeness of the plan and do not require derivation. Record a hard_defect only for a cited source that contradicts the intended use, or for an internal contradiction between entries. When candidate_completion_intent is true, also verify that the final dependency ledger reconciles with the intent ledger: a retired entry carries a reason; an intent entry that vanished silently is a hard_defect.
```

## Step references and dimensional closure

### The dependency ledger cites route slots (mechanism fix, always on)

#### Closure requirement 2 (`COMMON_WRITER_CLOSURE_REQUIREMENTS[1]`), replaced in full

```
Maintain and finally deliver a dependency ledger. Every approximation, every identity or formula not stated in the fixed task, every ingredient you drop or assert to vanish, and every choice of operator or Hamiltonian class that the result rests on must appear as one ledger entry with exactly one status: SOURCED (an exact span in the supplied allowlisted sources: source_id plus a short verbatim quote), DERIVED_HERE (the route position of the step that proves it from declared primitives, written as that step's slot number, for example "slot 3", or as "this step" when the step you are writing proves it; never a step revision id), or UNRESOLVED (explicitly open). A search hit, an uncited recollection, an analogy, or agreement in a special limit is not a source. The last step before complete must carry the full ledger in its derivation field. An ingredient the endpoint actually depends on that has no ledger entry, or a load-bearing UNRESOLVED entry, means the derivation is incomplete.
```

#### Checker addition (`CHECKER_DEVELOPER_INSTRUCTIONS`, always on)

```
Route steps are identified by slot. The transcript_catalog gives every step of the current route its step slot together with the superseded revision ids that slot has already had. A reference in a ledger entry or in an ancestor citation that names a step slot on the current route, or that names any superseded revision of such a slot, resolves to that slot's current revision and is never by itself a defect.
```

### Dimensional closure (switchable obligation)

Configuration: the per-run boolean `dimension_check` (default false).

#### Writer paragraph (only when the flag is on, directly after the dependency-ledger obligation)

```
Before declaring complete, the final step's derivation field must contain one explicit dimensional-analysis line for the endpoint formula, written in the unit system the derivation itself declares. State the dimension of every factor of that formula, show that the product of those dimensions equals the dimension of the quantity the task defines, and make every dimensional constant the check requires (for example hbar, c, e, 2*pi, a volume, or a normalisation) appear explicitly in the formula rather than leaving it implicit. A formula whose dimensions do not close is not complete.
```

#### The Checker's paragraph (only when the flag is on)

```
When candidate_completion_intent is true and the run declares dimension_check, recompute that dimensional-analysis line independently from the candidate's own declared definitions and unit system. Assign a dimension to every factor of the endpoint formula and confirm that the product equals the dimension of the quantity the task defines. A missing dimensional-analysis line, or a product whose dimensions do not close, is a hard_defect; quote the offending factor.
```

## Fork guidance and scope consistency

### Fork before reporting blocked (Writer, always on), plus the sentence for a cap of one

#### Fork guidance, Writer sentence (always on, directly after "Fork requires alternatives...")

```
When you would otherwise report blocked because a load-bearing ingredient is not determined by the declared inputs, first use fork with the non-equivalent closure routes you can name, including a route that restricts the declared operator or Hamiltonian class and says so; report blocked only when no such route remains.
```

#### Cap of one, Writer sentence (rendered only when `max_active_branches == 1`)

```
Forking is unavailable in this run and the output schema does not offer it, so where the rule above asks for a fork, choose instead between restricting the declared operator or Hamiltonian class and saying so, and reporting blocked.
```

### Trace of a suppressed fork (host behaviour plus a Writer runtime note)

#### Suppressed fork, Writer runtime note text

The host stores this text as the `guidance` of the runtime note it adds when it
suppresses a fork.

```
The run's active-branch limit was already reached, so no branch was created for these alternatives and nothing else will explore them. Continue on this branch: either carry one of them yourself and say which, or restrict the declared operator or Hamiltonian class and say so. Do not assume another branch covers them.
```

### Declared scope consistent with the derivation (Checker, always on, plus a closure-requirement sentence)

#### Scope consistency, Checker paragraph (always on)

```
When candidate_completion_intent is true, also read the candidate's declared scope against the derivation it rests on. For every operator class, Hamiltonian class, coupling, or approximation the scope claims to cover, confirm that some step of the route carries the term, construction, or argument that handles it. A class the scope claims with nothing in the derivation that handles it is a hard_defect: quote the scope sentence that makes the claim, and state that the candidate must either narrow the scope to the class the result rests on or derive the missing part.
```

#### Scope consistency, sentence appended to closure requirement 7 (`COMMON_WRITER_CLOSURE_REQUIREMENTS[6]`)

```
At completion the Checker reads the class you state against the derivation, so every operator class, Hamiltonian class, coupling, or approximation your claimed range covers must have the term, construction, or argument that handles it somewhere in the route.
```
