"""Generic product closure obligations, shared with prepared research contexts."""

COMMON_WRITER_CLOSURE_REQUIREMENTS = (
    (
        "Carry the starting object you declare through to the explicit "
        "computable endpoint the task defines. A newly named object, vertex, "
        "covariant derivative, or formal derivative is not a closed result "
        "unless it is reduced to the input data the task declares."
    ),
    (
        "Maintain and finally deliver a dependency ledger. Every "
        "approximation, every identity or formula not stated in the fixed "
        "task, every ingredient you drop or assert to vanish, and every "
        "choice of operator or Hamiltonian class that the result rests on "
        "must appear as one ledger entry with exactly one status: SOURCED (an "
        "exact span in the supplied allowlisted sources: source_id plus a "
        "short verbatim quote), DERIVED_HERE (the route position of the step "
        "that proves it from declared primitives, written as that step's slot "
        'number, for example "slot 3", or as "this step" when the step '
        "you are writing proves it; never a step revision id), or "
        "UNRESOLVED (explicitly open). A search hit, an uncited recollection, "
        "an analogy, or agreement in a special limit is not a source. The "
        "last step before complete must carry the full ledger in its "
        "derivation field. An ingredient the endpoint actually depends on "
        "that has no ledger entry, or a load-bearing UNRESOLVED entry, means "
        "the derivation is incomplete."
    ),
    (
        "Make every construction computable under the boundary conditions the "
        "task declares: define every projector, gauge choice, integration "
        "measure, weight, and every derivative or finite-difference limit you "
        "use. Never use an operator that is ill-defined under those boundary "
        "conditions without a stated regularization."
    ),
    (
        "Derive the full result the task's scope demands, including every part "
        "of it that your declared approximation requires; keeping only the "
        "part that is convenient in one regime and dropping a part the scope "
        "covers is incomplete. Audit overall signs and factors against your "
        "own stated conventions with an explicit consistency check."
    ),
    (
        "Close every constant, normalization, weight, free index, complex "
        "conjugation, and unit convention in the final formula. Treat "
        "degenerate subspaces with projectors or matrix resolvents, not "
        "nondegenerate denominators."
    ),
    (
        "Show the symmetry and limiting checks appropriate to the declared "
        "result, and the conversion to the quantities the task names. For "
        "every limit the declared scope names (for example a limit in which a "
        "declared variable tends to zero or to infinity), write the "
        "leading-order expansion of the final formula in that limit "
        "explicitly in the final step, naming each part of the formula the "
        "scope requires that you retain, or the rule that completes a part "
        "you omit; a declared limit whose behaviour is asserted rather than "
        "written out is not closed. List only genuinely non-load-bearing "
        "unresolved issues; any missing ingredient needed to compute the "
        "endpoint means the derivation is incomplete."
    ),
    (
        "State the class of Hamiltonian and operators the result actually "
        "rests on, and keep the final claimed range of validity no wider than "
        "that class; if the starting object is more general than the "
        "operators you then use, either derive the additional pieces the "
        "general case requires or restrict the claim explicitly. An unproved "
        "exclusion and a deferred implementation note are unresolved issues, "
        "not closed steps. At completion the Checker reads the class you state "
        "against the derivation, so every operator class, Hamiltonian class, "
        "coupling, or approximation your claimed range covers must have the "
        "term, construction, or argument that handles it somewhere in the route."
    ),
)
