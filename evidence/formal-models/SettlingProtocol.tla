--------------------------- MODULE SettlingProtocol ----------------------------
(*
 * State-machine specification of pyropdf-store's multi-round settling protocol
 * with savepoint rollback, scope filtering, and cycle refusal (DESIGN.md
 * section 7; crates/pyropdf-store/src/settle.rs & version/transaction.rs).
 *
 * Structures follow `Structure::ALL = <<Appearance, Dependent, FontSubset>>`.
 *   - `Scope = "One(Appearance)"` covers only `Appearance`
 *   - `Scope = "One(Dependent)"` covers only `Dependent`
 *   - `Scope = "Commit"` covers `Appearance` and `Dependent` (never `FontSubset`)
 *   - `Scope = "Save"` covers all three structures
 *
 * Settlers may write objects, allocate/advance `hwm`, mark downstream or
 * upstream structures stale (including cycles), or fail with an error.
 *
 * Key invariants checked exhaustively by TLC:
 *   1. NoUnsettledOnSuccess: after `settle(scope)` succeeds, no structure
 *      covered by `scope` with a registered settler remains stale.
 *   2. FontSubsetNeverSettledBeforeSave: `One(_)` and `Commit` scopes never
 *      clear a `FontSubset` mark.
 *   3. AtomicRollbackOnError: if `settle(scope)` fails (settler error or
 *      cycle exceeding `MAX_ROUNDS`), `draftVer` and `stale` match the exact
 *      pre-call savepoint while `hwm` never decreases.
 *   4. BoundedTermination: round counter never exceeds `MAX_ROUNDS`.
 *)
EXTENDS Integers, Sequences, FiniteSets

CONSTANTS
    MaxDraftVer,   \* Bound on draft mutations (e.g. 5)
    MaxHwm         \* Bound on high-water mark (e.g. 5)

Structures == {"Appearance", "Dependent", "FontSubset"}
Order == <<"Appearance", "Dependent", "FontSubset">>
MaxRounds == 2 * Len(Order)   \* Matches `settle::MAX_ROUNDS = 2 * Structure::ALL.len()`
Scopes == {"OneA", "OneD", "Commit", "Save"}

Covers(scope, s) ==
    CASE scope = "OneA"   -> s = "Appearance"
      [] scope = "OneD"   -> s = "Dependent"
      [] scope = "Commit" -> s /= "FontSubset"
      [] scope = "Save"   -> TRUE

VARIABLES
    registered,       \* Subset of Structures with registered settlers
    stale,            \* Currently stale structures in draft
    draftVer,         \* Abstract content state of transaction draft
    hwm,              \* Monotonic allocation high-water mark
    \* Active `Transaction::settle` execution state:
    phase,            \* "idle" | "running" | "done_ok" | "done_err"
    activeScope,
    round,            \* 0..MaxRounds
    idx,              \* 1..Len(Order)+1 within current round
    settledInRound,   \* BOOLEAN
    \* Savepoint snapshot taken at `Transaction::settle` entry:
    spStale,
    spDraftVer,
    spHwm

vars == <<
    registered, stale, draftVer, hwm,
    phase, activeScope, round, idx, settledInRound,
    spStale, spDraftVer, spHwm
>>

Init ==
    /\ registered \in { {"Appearance", "FontSubset"}, Structures }
    /\ stale \in SUBSET Structures
    /\ draftVer = 0
    /\ hwm = 1
    /\ phase = "idle"
    /\ activeScope = "Commit"
    /\ round = 0
    /\ idx = 1
    /\ settledInRound = FALSE
    /\ spStale = stale
    /\ spDraftVer = 0
    /\ spHwm = 1

\* Start `Transaction::settle(scope)` from idle (including early return when
\* nothing covered is stale outside `Save`).
StartSettle(scope) ==
    /\ phase = "idle"
    /\ activeScope' = scope
    /\ spStale' = stale
    /\ spDraftVer' = draftVer
    /\ spHwm' = hwm
    /\ IF scope /= "Save" /\ (\A s \in Structures: ~(Covers(scope, s) /\ s \in stale)) THEN
           /\ phase' = "done_ok"
           /\ round' = 0
           /\ idx' = 1
           /\ settledInRound' = FALSE
       ELSE
           /\ phase' = "running"
           /\ round' = 0
           /\ idx' = 1
           /\ settledInRound' = FALSE
    /\ UNCHANGED <<registered, stale, draftVer, hwm>>

\* Advance one structure slot inside `settle()`'s inner loop.
StepStructure ==
    /\ phase = "running"
    /\ round < MaxRounds
    /\ idx <= Len(Order)
    /\ LET s == Order[idx]
           active == Covers(activeScope, s) /\ (s \in registered) /\ (s \in stale)
       IN IF ~active THEN
              /\ idx' = idx + 1
              /\ UNCHANGED <<registered, stale, draftVer, hwm, phase, activeScope,
                             round, settledInRound, spStale, spDraftVer, spHwm>>
          ELSE
              \* Settler runs: either succeeds (writing draft, possibly bumping
              \* hwm, and marking zero or more structures — including downstream
              \* or cyclic marks) OR fails (triggering atomic rollback).
              \/ \* Outcome 1: succeeds and marks a subset `addedMarks`
                 \E addedMarks \in SUBSET Structures, bumpHwm \in {0, 1}:
                    /\ draftVer < MaxDraftVer
                    /\ hwm + bumpHwm <= MaxHwm
                    \* Realistic cascade: Appearance may mark Dependent or FontSubset;
                    \* Dependent may mark FontSubset or (to test cycles) Appearance;
                    \* FontSubset marks nothing or (to test cycles) Appearance.
                    /\ addedMarks \in { {}, {"FontSubset"}, {"Dependent"}, {"Appearance"} }
                    /\ stale' = (stale \ {s}) \cup addedMarks
                    /\ draftVer' = draftVer + 1
                    /\ hwm' = hwm + bumpHwm
                    /\ idx' = idx + 1
                    /\ settledInRound' = TRUE
                    /\ UNCHANGED <<registered, phase, activeScope, round,
                                   spStale, spDraftVer, spHwm>>
              \/ \* Outcome 2: settler refuses with error -> rollback to savepoint!
                 \E bumpHwm \in {0, 1}:
                    /\ hwm + bumpHwm <= MaxHwm
                    /\ stale' = spStale
                    /\ draftVer' = spDraftVer
                    /\ hwm' = hwm + bumpHwm
                    /\ phase' = "done_err"
                    /\ UNCHANGED <<registered, activeScope, round, idx, settledInRound,
                                   spStale, spDraftVer, spHwm>>

\* End of one inner pass over `Structure::ALL`: stop if nothing settled, else
\* start next round or check for unfinished cycle after `MaxRounds`.
FinishRound ==
    /\ phase = "running"
    /\ idx = Len(Order) + 1
    /\ IF ~settledInRound THEN
           /\ phase' = "done_ok"
           /\ UNCHANGED <<registered, stale, draftVer, hwm, activeScope, round,
                          idx, settledInRound, spStale, spDraftVer, spHwm>>
       ELSE IF round + 1 < MaxRounds THEN
           /\ round' = round + 1
           /\ idx' = 1
           /\ settledInRound' = FALSE
           /\ UNCHANGED <<registered, stale, draftVer, hwm, phase, activeScope,
                          spStale, spDraftVer, spHwm>>
       ELSE
           \* Completed `MaxRounds` rounds: check if any covered registered
           \* structure is still stale (cycle -> rollback and error).
           LET stillStale == \E s \in Structures:
                                Covers(activeScope, s) /\ s \in registered /\ s \in stale
           IN IF stillStale THEN
                  /\ stale' = spStale
                  /\ draftVer' = spDraftVer
                  /\ phase' = "done_err"
                  /\ round' = MaxRounds
                  /\ UNCHANGED <<registered, hwm, activeScope, idx, settledInRound,
                                 spStale, spDraftVer, spHwm>>
              ELSE
                  /\ phase' = "done_ok"
                  /\ round' = MaxRounds
                  /\ UNCHANGED <<registered, stale, draftVer, hwm, activeScope,
                                 idx, settledInRound, spStale, spDraftVer, spHwm>>

\* Allow a completed settle to return to idle and optionally receive a new mark
\* before another settle call (models interleaved read-write loops).
ResetToIdle ==
    /\ phase \in {"done_ok", "done_err"}
    /\ phase' = "idle"
    /\ UNCHANGED <<registered, stale, draftVer, hwm, activeScope, round, idx,
                   settledInRound, spStale, spDraftVer, spHwm>>

Next ==
    \/ \E scope \in Scopes: StartSettle(scope)
    \/ StepStructure
    \/ FinishRound
    \/ ResetToIdle

Spec == Init /\ [][Next]_vars

\* ---- Invariants ----

BoundedRounds ==
    round <= MaxRounds

HwmNeverDecreases ==
    hwm >= spHwm

NoUnsettledOnSuccess ==
    (phase = "done_ok") =>
        \A s \in Structures:
            (Covers(activeScope, s) /\ s \in registered) => ~(s \in stale)

FontSubsetNeverClearedOutsideSave ==
    (phase = "done_ok" /\ activeScope /= "Save" /\ "FontSubset" \in spStale) =>
        ("FontSubset" \in stale)

AtomicRollbackOnError ==
    (phase = "done_err") =>
        /\ stale = spStale
        /\ draftVer = spDraftVer
        /\ hwm >= spHwm

================================================================================
