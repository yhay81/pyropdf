------------------------- MODULE HistoryAndSaveNumbers -------------------------
(*
 * State-machine specification of pyropdf-store's version history, memory
 * budget eviction, monotonic object-number allocation, savepoint rollback,
 * deterministic save numbering (`SaveNumbers`), and `restore_history` label
 * synchronization (DESIGN.md section 4.1; crates/pyropdf-store/src/version/
 * {document,transaction}.rs).
 *
 * Transaction-local fields are reset to canonical defaults whenever a
 * transaction or savepoint closes so TLC fingerprints only live state without
 * losing any semantic transition.
 *)
EXTENDS Integers, Sequences, FiniteSets

CONSTANTS
    BaseNext,        \* First free object number above the base (e.g. 3)
    MaxHwm,          \* Bound on document high-water mark (e.g. 6)
    MaxSteps,        \* Bound on history length (e.g. 4)
    MaxCommits,      \* Bound on total commits (e.g. 3)
    Weights,         \* Set of step memory weights (e.g. {1, 2})
    Limits           \* Set of finite memory limits + INF (e.g. {0, 2, 4, 99})

INF == 99

VARIABLES
    steps,
    pos,
    size,
    redoSize,
    limit,
    labels,
    nextLabelId,
    hwm,
    allocatedEver,
    txOpen,
    txParentNext,
    txDraftNext,
    txDirty,
    txHasSavepoint,
    txSpDraftNext,
    txSpDirty,
    saveTakenStart,
    saveTakenEnd,
    lastCleanSaveNums,
    lastCleanSaveVer,
    \* Records whether any allocation collision or save collision occurred.
    allocCollision,
    saveCollision

vars == <<
    steps, pos, size, redoSize, limit, labels, nextLabelId,
    hwm, allocatedEver,
    txOpen, txParentNext, txDraftNext, txDirty,
    txHasSavepoint, txSpDraftNext, txSpDirty,
    saveTakenStart, saveTakenEnd,
    lastCleanSaveNums, lastCleanSaveVer,
    allocCollision, saveCollision
>>

RECURSIVE SumRange(_, _, _)
SumRange(f, a, b) ==
    IF a > b THEN 0
    ELSE f[a] + SumRange(f, a + 1, b)

UndoCost(s, p) ==
    LET f == [i \in 1..Len(s) |-> s[i].released]
    IN SumRange(f, 2, p)

RedoCost(s, p) ==
    LET f == [i \in 1..Len(s) |-> s[i].added]
    IN SumRange(f, p + 1, Len(s))

RECURSIVE FitRedo(_, _, _, _, _)
FitRedo(s, p, sz, rsz, lim) ==
    IF lim = INF \/ rsz <= lim \/ Len(s) <= p THEN
        [steps |-> s, pos |-> p, size |-> sz, redoSize |-> rsz]
    ELSE
        LET newest == s[Len(s)]
            s2 == SubSeq(s, 1, Len(s) - 1)
            rsz2 == rsz - newest.added
            sz2 == sz - newest.added
        IN FitRedo(s2, p, sz2, rsz2, lim)

RECURSIVE FitUndo(_, _, _, _, _)
FitUndo(s, p, sz, rsz, lim) ==
    IF lim = INF \/ sz <= lim \/ p <= 1 THEN
        [steps |-> s, pos |-> p, size |-> sz, redoSize |-> rsz]
    ELSE
        LET sTail == SubSeq(s, 2, Len(s))
            droppedReleased == sTail[1].released
            zeroedHead == [sTail[1] EXCEPT !.added = 0, !.released = 0]
            s2 == <<zeroedHead>> \o SubSeq(sTail, 2, Len(sTail))
        IN FitUndo(s2, p - 1, sz - droppedReleased, rsz, lim)

Fit(s, p, sz, rsz, lim) ==
    LET r == FitRedo(s, p, sz, rsz, lim)
    IN FitUndo(r.steps, r.pos, r.size, r.redoSize, lim)

SyncLabels(origLabels, origPos, newSteps, newPos) ==
    LET oldUndo == origPos - 1
        newUndo == newPos - 1
        newRedo == Len(newSteps) - newPos
        droppedUndo == oldUndo - newUndo
        keptEnd == oldUndo + newRedo
    IN SubSeq(origLabels, droppedUndo + 1, keptEnd)

Max2(a, b) == IF a >= b THEN a ELSE b

ResetTx ==
    /\ txOpen' = FALSE
    /\ txParentNext' = BaseNext
    /\ txDraftNext' = BaseNext
    /\ txDirty' = FALSE
    /\ txHasSavepoint' = FALSE
    /\ txSpDraftNext' = BaseNext
    /\ txSpDirty' = FALSE
    /\ saveTakenStart' = BaseNext
    /\ saveTakenEnd' = BaseNext

Init ==
    /\ steps = <<[ver |-> 0, added |-> 0, released |-> 0, next |-> BaseNext, label |-> 0]>>
    /\ pos = 1
    /\ size = 0
    /\ redoSize = 0
    /\ limit = INF
    /\ labels = <<>>
    /\ nextLabelId = 1
    /\ hwm = BaseNext
    /\ allocatedEver = {}
    /\ txOpen = FALSE
    /\ txParentNext = BaseNext
    /\ txDraftNext = BaseNext
    /\ txDirty = FALSE
    /\ txHasSavepoint = FALSE
    /\ txSpDraftNext = BaseNext
    /\ txSpDirty = FALSE
    /\ saveTakenStart = BaseNext
    /\ saveTakenEnd = BaseNext
    /\ lastCleanSaveNums = <<>>
    /\ lastCleanSaveVer = -1
    /\ allocCollision = FALSE
    /\ saveCollision = FALSE

BeginTx ==
    /\ ~txOpen
    /\ txOpen' = TRUE
    /\ txParentNext' = steps[pos].next
    /\ txDraftNext' = Max2(steps[pos].next, hwm)
    /\ txDirty' = FALSE
    /\ txHasSavepoint' = FALSE
    /\ txSpDraftNext' = BaseNext
    /\ txSpDirty' = FALSE
    /\ saveTakenStart' = BaseNext
    /\ saveTakenEnd' = BaseNext
    /\ UNCHANGED <<steps, pos, size, redoSize, limit, labels, nextLabelId,
                   hwm, allocatedEver, lastCleanSaveNums, lastCleanSaveVer,
                   allocCollision, saveCollision>>

AllocateInTx ==
    /\ txOpen
    /\ LET n == Max2(txDraftNext, hwm)
       IN /\ n < MaxHwm
          /\ allocCollision' = (allocCollision \/ (n \in allocatedEver))
          /\ txDraftNext' = n + 1
          /\ hwm' = Max2(hwm, n + 1)
          /\ allocatedEver' = allocatedEver \cup {n}
          /\ UNCHANGED <<steps, pos, size, redoSize, limit, labels, nextLabelId,
                         txOpen, txParentNext, txDirty, txHasSavepoint,
                         txSpDraftNext, txSpDirty, saveTakenStart, saveTakenEnd,
                         lastCleanSaveNums, lastCleanSaveVer, saveCollision>>

WriteInTx ==
    /\ txOpen
    /\ ~txDirty
    /\ txDirty' = TRUE
    /\ UNCHANGED <<steps, pos, size, redoSize, limit, labels, nextLabelId,
                   hwm, allocatedEver, txOpen, txParentNext, txDraftNext,
                   txHasSavepoint, txSpDraftNext, txSpDirty,
                   saveTakenStart, saveTakenEnd, lastCleanSaveNums, lastCleanSaveVer,
                   allocCollision, saveCollision>>

TakeSavepoint ==
    /\ txOpen
    /\ ~txHasSavepoint
    /\ txHasSavepoint' = TRUE
    /\ txSpDraftNext' = txDraftNext
    /\ txSpDirty' = txDirty
    /\ UNCHANGED <<steps, pos, size, redoSize, limit, labels, nextLabelId,
                   hwm, allocatedEver, txOpen, txParentNext, txDraftNext, txDirty,
                   saveTakenStart, saveTakenEnd, lastCleanSaveNums, lastCleanSaveVer,
                   allocCollision, saveCollision>>

RollbackSavepoint ==
    /\ txOpen
    /\ txHasSavepoint
    /\ txHasSavepoint' = FALSE
    /\ txDraftNext' = Max2(txSpDraftNext, txDraftNext)
    /\ txDirty' = txSpDirty
    /\ txSpDraftNext' = BaseNext
    /\ txSpDirty' = FALSE
    /\ UNCHANGED <<steps, pos, size, redoSize, limit, labels, nextLabelId,
                   hwm, allocatedEver, txOpen, txParentNext,
                   saveTakenStart, saveTakenEnd, lastCleanSaveNums, lastCleanSaveVer,
                   allocCollision, saveCollision>>

SaveSettleInTx ==
    /\ txOpen
    /\ IF ~txDirty THEN
           LET num == txParentNext
               curVer == steps[pos].ver
           IN /\ num < MaxHwm
              /\ saveCollision' = (saveCollision \/ (num < txParentNext))
              /\ hwm' = Max2(hwm, num + 1)
              /\ lastCleanSaveVer' = curVer
              /\ lastCleanSaveNums' = <<num>>
              /\ UNCHANGED <<steps, pos, size, redoSize, limit, labels, nextLabelId,
                             allocatedEver, txOpen, txParentNext, txDraftNext, txDirty,
                             txHasSavepoint, txSpDraftNext, txSpDirty,
                             saveTakenStart, saveTakenEnd, allocCollision>>
       ELSE
           LET free == (saveTakenEnd = hwm) /\ (saveTakenStart < saveTakenEnd)
               start == IF free THEN saveTakenStart ELSE hwm
               num == start
           IN /\ num < MaxHwm
              /\ saveCollision' = (saveCollision \/ (num < txParentNext))
              /\ saveTakenStart' = start
              /\ saveTakenEnd' = Max2(IF free THEN saveTakenEnd ELSE start, num + 1)
              /\ hwm' = Max2(hwm, num + 1)
              /\ UNCHANGED <<steps, pos, size, redoSize, limit, labels, nextLabelId,
                             allocatedEver, txOpen, txParentNext, txDraftNext, txDirty,
                             txHasSavepoint, txSpDraftNext, txSpDirty,
                             lastCleanSaveNums, lastCleanSaveVer, allocCollision>>

DropTx ==
    /\ txOpen
    /\ ResetTx
    /\ UNCHANGED <<steps, pos, size, redoSize, limit, labels, nextLabelId,
                   hwm, allocatedEver, lastCleanSaveNums, lastCleanSaveVer,
                   allocCollision, saveCollision>>

CommitTx(addW, relW) ==
    /\ txOpen
    /\ txDirty
    /\ Len(steps) < MaxSteps
    /\ nextLabelId <= MaxCommits
    /\ LET publishedNext == Max2(txDraftNext, hwm)
           prefix == SubSeq(steps, 1, pos)
           prefixLabels == SubSeq(labels, 1, pos - 1)
           newStep == [ver |-> nextLabelId, added |-> addW, released |-> relW,
                       next |-> publishedNext, label |-> nextLabelId]
           s1 == Append(prefix, newStep)
           p1 == pos + 1
           sz1 == (size - redoSize) + relW
           fit == Fit(s1, p1, sz1, 0, limit)
           l1 == Append(prefixLabels, nextLabelId)
       IN /\ steps' = fit.steps
          /\ pos' = fit.pos
          /\ size' = fit.size
          /\ redoSize' = fit.redoSize
          /\ labels' = SyncLabels(l1, p1, fit.steps, fit.pos)
          /\ nextLabelId' = nextLabelId + 1
          /\ hwm' = Max2(hwm, publishedNext)
          /\ ResetTx
          /\ UNCHANGED <<limit, allocatedEver, lastCleanSaveNums, lastCleanSaveVer,
                         allocCollision, saveCollision>>

Undo ==
    /\ ~txOpen
    /\ pos > 1
    /\ LET cur == steps[pos]
           sz1 == (size - cur.released) + cur.added
           rsz1 == redoSize + cur.added
           p1 == pos - 1
           fit == Fit(steps, p1, sz1, rsz1, limit)
       IN /\ steps' = fit.steps
          /\ pos' = fit.pos
          /\ size' = fit.size
          /\ redoSize' = fit.redoSize
          /\ labels' = SyncLabels(labels, p1, fit.steps, fit.pos)
          /\ UNCHANGED <<limit, nextLabelId, hwm, allocatedEver,
                         txOpen, txParentNext, txDraftNext, txDirty,
                         txHasSavepoint, txSpDraftNext, txSpDirty,
                         saveTakenStart, saveTakenEnd, lastCleanSaveNums,
                         lastCleanSaveVer, allocCollision, saveCollision>>

Redo ==
    /\ ~txOpen
    /\ pos < Len(steps)
    /\ LET nxt == steps[pos + 1]
           sz1 == (size - nxt.added) + nxt.released
           rsz1 == redoSize - nxt.added
           p1 == pos + 1
           fit == Fit(steps, p1, sz1, rsz1, limit)
       IN /\ steps' = fit.steps
          /\ pos' = fit.pos
          /\ size' = fit.size
          /\ redoSize' = fit.redoSize
          /\ labels' = SyncLabels(labels, p1, fit.steps, fit.pos)
          /\ UNCHANGED <<limit, nextLabelId, hwm, allocatedEver,
                         txOpen, txParentNext, txDraftNext, txDirty,
                         txHasSavepoint, txSpDraftNext, txSpDirty,
                         saveTakenStart, saveTakenEnd, lastCleanSaveNums,
                         lastCleanSaveVer, allocCollision, saveCollision>>

\* Models both `set_history_limit(newLim)` and `save_history()` +
\* `restore_history()` into a freshly opened document with limit `newLim`.
SetOrRestoreUnderLimit(newLim) ==
    /\ ~txOpen
    /\ newLim /= limit
    /\ LET sz0 == UndoCost(steps, pos) + RedoCost(steps, pos)
           rsz0 == RedoCost(steps, pos)
           fit == Fit(steps, pos, sz0, rsz0, newLim)
           maxNext == steps[Len(steps)].next
       IN /\ limit' = newLim
          /\ steps' = fit.steps
          /\ pos' = fit.pos
          /\ size' = fit.size
          /\ redoSize' = fit.redoSize
          /\ labels' = SyncLabels(labels, pos, fit.steps, fit.pos)
          /\ hwm' = Max2(hwm, maxNext)
          /\ UNCHANGED <<nextLabelId, allocatedEver,
                         txOpen, txParentNext, txDraftNext, txDirty,
                         txHasSavepoint, txSpDraftNext, txSpDirty,
                         saveTakenStart, saveTakenEnd, lastCleanSaveNums,
                         lastCleanSaveVer, allocCollision, saveCollision>>

Next ==
    \/ BeginTx
    \/ AllocateInTx
    \/ WriteInTx
    \/ TakeSavepoint
    \/ RollbackSavepoint
    \/ SaveSettleInTx
    \/ DropTx
    \/ \E a \in Weights, r \in Weights: CommitTx(a, r)
    \/ Undo
    \/ Redo
    \/ \E l \in Limits: SetOrRestoreUnderLimit(l)

Spec == Init /\ [][Next]_vars

\* ---- Invariants ----

TypeOK ==
    /\ Len(steps) >= 1
    /\ pos \in 1..Len(steps)
    /\ size >= 0
    /\ redoSize >= 0
    /\ hwm >= BaseNext
    /\ Len(labels) = Len(steps) - 1

NoAllocationOrSaveCollision ==
    /\ ~allocCollision
    /\ ~saveCollision

HistoryAccountingExact ==
    /\ steps[1].added = 0
    /\ steps[1].released = 0
    /\ redoSize = RedoCost(steps, pos)
    /\ size = UndoCost(steps, pos) + redoSize
    /\ (limit /= INF => size <= limit)

HwmAboveAllAllocatedAndVersions ==
    /\ \A n \in allocatedEver: n < hwm
    /\ \A i \in 1..Len(steps): steps[i].next <= hwm

LabelsMatchTransitions ==
    /\ Len(labels) = Len(steps) - 1
    /\ \A i \in 1..Len(labels): labels[i] = steps[i + 1].label

CleanSaveMatchesParentNext ==
    (lastCleanSaveVer = steps[pos].ver) =>
        lastCleanSaveNums = <<steps[pos].next>>

================================================================================
