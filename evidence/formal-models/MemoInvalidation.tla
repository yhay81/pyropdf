--------------------------- MODULE MemoInvalidation ----------------------------
(*
 * State-machine specification of pyropdf-store's F4 read-set memoization,
 * `ChangeSet` disjointness, `Weak<Version>` expiry, concurrent `Memo::reuse`
 * pointer advancement, and `ObjectStamps` (DESIGN.md section 5;
 * crates/pyropdf-store/src/memo/mod.rs & version/stamps.rs).
 *
 * Thread-local snapshot fields are reset to canonical defaults whenever a
 * thread returns to `"idle"` or enters `"computing"`, and `staleRead` records
 * any mismatch immediately on return so TLC explores every interleaving
 * without ghost-state blowup.
 *)
EXTENDS Integers, Sequences, FiniteSets

CONSTANTS
    Objects,       \* e.g. {1, 2}
    Vals,          \* e.g. {0, 1}
    MaxVersions,   \* e.g. 3
    Threads        \* e.g. {"T1", "T2"}

QueryReadSet(state) ==
    IF state[1] = 1 THEN {1, 2} ELSE {1}

QueryEval(state) ==
    IF state[1] = 1 THEN state[1] * 10 + state[2] ELSE state[1]

VARIABLES
    versions,
    hist,
    histPos,
    memoHas,
    memoVer,
    memoReads,
    memoVal,
    memoValId,
    pc,
    tTargetVer,
    tSnapVer,
    tSnapReads,
    tSnapVal,
    tSnapValId,
    staleRead,
    stampHas,
    stampMap,
    stampReads,
    stampVal

vars == <<
    versions, hist, histPos,
    memoHas, memoVer, memoReads, memoVal, memoValId,
    pc, tTargetVer, tSnapVer, tSnapReads, tSnapVal, tSnapValId, staleRead,
    stampHas, stampMap, stampReads, stampVal
>>

ChangeSet(v1, v2) ==
    {o \in Objects : versions[v1].stamps[o] /= versions[v2].stamps[o]}

ValIdPool == 1..(Cardinality(Threads) + 2)

FreshValId ==
    CHOOSE id \in ValIdPool:
        /\ (~memoHas \/ id /= memoValId)
        /\ \A u \in Threads: (pc[u] \notin {"reuse_check", "reuse_relock"} \/ id /= tSnapValId[u])

InitState == [o \in Objects |-> 0]
InitStamps == [o \in Objects |-> 0]

ClearThreadSnap(t) ==
    /\ tSnapVer' = [tSnapVer EXCEPT ![t] = 0]
    /\ tSnapReads' = [tSnapReads EXCEPT ![t] = {}]
    /\ tSnapVal' = [tSnapVal EXCEPT ![t] = 0]
    /\ tSnapValId' = [tSnapValId EXCEPT ![t] = 0]

ReturnIdle(t, returnedVal) ==
    LET target == tTargetVer[t]
    IN /\ staleRead' = (staleRead \/ (returnedVal /= QueryEval(versions[target].state)))
       /\ pc' = [pc EXCEPT ![t] = "idle"]
       /\ tTargetVer' = [tTargetVer EXCEPT ![t] = 0]
       /\ ClearThreadSnap(t)

Init ==
    /\ versions = <<[state |-> InitState, stamps |-> InitStamps, alive |-> TRUE]>>
    /\ hist = <<1>>
    /\ histPos = 1
    /\ memoHas = FALSE
    /\ memoVer = 0
    /\ memoReads = {}
    /\ memoVal = 0
    /\ memoValId = 0
    /\ pc = [t \in Threads |-> "idle"]
    /\ tTargetVer = [t \in Threads |-> 0]
    /\ tSnapVer = [t \in Threads |-> 0]
    /\ tSnapReads = [t \in Threads |-> {}]
    /\ tSnapVal = [t \in Threads |-> 0]
    /\ tSnapValId = [t \in Threads |-> 0]
    /\ staleRead = FALSE
    /\ stampHas = FALSE
    /\ stampMap = InitStamps
    /\ stampReads = {}
    /\ stampVal = 0

CommitEdit(o, val) ==
    /\ Len(versions) < MaxVersions
    /\ LET curIdx == hist[histPos]
           cur == versions[curIdx]
           newIdx == Len(versions) + 1
           newVer == [
               state |-> [cur.state EXCEPT ![o] = val],
               stamps |-> [cur.stamps EXCEPT ![o] = newIdx],
               alive |-> TRUE
           ]
       IN /\ versions' = Append(versions, newVer)
          /\ hist' = Append(SubSeq(hist, 1, histPos), newIdx)
          /\ histPos' = histPos + 1
          /\ UNCHANGED <<memoHas, memoVer, memoReads, memoVal, memoValId,
                         pc, tTargetVer, tSnapVer, tSnapReads, tSnapVal, tSnapValId,
                         staleRead, stampHas, stampMap, stampReads, stampVal>>

Undo ==
    /\ histPos > 1
    /\ histPos' = histPos - 1
    /\ UNCHANGED <<versions, hist, memoHas, memoVer, memoReads, memoVal, memoValId,
                   pc, tTargetVer, tSnapVer, tSnapReads, tSnapVal,
                   tSnapValId, staleRead, stampHas, stampMap, stampReads, stampVal>>

Redo ==
    /\ histPos < Len(hist)
    /\ histPos' = histPos + 1
    /\ UNCHANGED <<versions, hist, memoHas, memoVer, memoReads, memoVal, memoValId,
                   pc, tTargetVer, tSnapVer, tSnapReads, tSnapVal,
                   tSnapValId, staleRead, stampHas, stampMap, stampReads, stampVal>>

ExpireWeakVersion(vIdx) ==
    /\ vIdx \in 1..Len(versions)
    /\ versions[vIdx].alive
    /\ vIdx /= hist[histPos]
    /\ \A t \in Threads: tTargetVer[t] /= vIdx /\ tSnapVer[t] /= vIdx
    /\ versions' = [versions EXCEPT ![vIdx].alive = FALSE]
    /\ UNCHANGED <<hist, histPos, memoHas, memoVer, memoReads, memoVal, memoValId,
                   pc, tTargetVer, tSnapVer, tSnapReads, tSnapVal,
                   tSnapValId, staleRead, stampHas, stampMap, stampReads, stampVal>>

StartLookup(t) ==
    /\ pc[t] = "idle"
    /\ LET target == hist[histPos]
       IN /\ tTargetVer' = [tTargetVer EXCEPT ![t] = target]
          /\ IF memoHas THEN
                 /\ pc' = [pc EXCEPT ![t] = "reuse_check"]
                 /\ tSnapVer' = [tSnapVer EXCEPT ![t] = memoVer]
                 /\ tSnapReads' = [tSnapReads EXCEPT ![t] = memoReads]
                 /\ tSnapVal' = [tSnapVal EXCEPT ![t] = memoVal]
                 /\ tSnapValId' = [tSnapValId EXCEPT ![t] = memoValId]
             ELSE
                 /\ pc' = [pc EXCEPT ![t] = "computing"]
                 /\ UNCHANGED <<tSnapVer, tSnapReads, tSnapVal, tSnapValId>>
          /\ UNCHANGED <<versions, hist, histPos, memoHas, memoVer, memoReads,
                         memoVal, memoValId, staleRead,
                         stampHas, stampMap, stampReads, stampVal>>

ReuseCheck(t) ==
    /\ pc[t] = "reuse_check"
    /\ LET held == tSnapVer[t]
           target == tTargetVer[t]
       IN IF ~versions[held].alive THEN
              /\ pc' = [pc EXCEPT ![t] = "computing"]
              /\ ClearThreadSnap(t)
              /\ UNCHANGED <<versions, hist, histPos, memoHas, memoVer, memoReads,
                             memoVal, memoValId, tTargetVer, staleRead,
                             stampHas, stampMap, stampReads, stampVal>>
          ELSE IF held = target THEN
              /\ ReturnIdle(t, tSnapVal[t])
              /\ UNCHANGED <<versions, hist, histPos, memoHas, memoVer, memoReads,
                             memoVal, memoValId, stampHas, stampMap, stampReads, stampVal>>
          ELSE IF tSnapReads[t] \cap ChangeSet(held, target) = {} THEN
              /\ pc' = [pc EXCEPT ![t] = "reuse_relock"]
              /\ UNCHANGED <<versions, hist, histPos, memoHas, memoVer, memoReads,
                             memoVal, memoValId, tTargetVer, tSnapVer,
                             tSnapReads, tSnapVal, tSnapValId, staleRead,
                             stampHas, stampMap, stampReads, stampVal>>
          ELSE
              /\ pc' = [pc EXCEPT ![t] = "computing"]
              /\ ClearThreadSnap(t)
              /\ UNCHANGED <<versions, hist, histPos, memoHas, memoVer, memoReads,
                             memoVal, memoValId, tTargetVer, staleRead,
                             stampHas, stampMap, stampReads, stampVal>>

ReuseRelock(t) ==
    /\ pc[t] = "reuse_relock"
    /\ IF memoHas /\ memoValId = tSnapValId[t] THEN
           memoVer' = tTargetVer[t]
       ELSE
           UNCHANGED memoVer
    /\ ReturnIdle(t, tSnapVal[t])
    /\ UNCHANGED <<versions, hist, histPos, memoHas, memoReads, memoVal, memoValId,
                   stampHas, stampMap, stampReads, stampVal>>

FinishCompute(t) ==
    /\ pc[t] = "computing"
    /\ LET target == tTargetVer[t]
           st == versions[target].state
           rset == QueryReadSet(st)
           val == QueryEval(st)
           newId == FreshValId
       IN /\ memoHas' = TRUE
          /\ memoVer' = target
          /\ memoReads' = rset
          /\ memoVal' = val
          /\ memoValId' = newId
          /\ ReturnIdle(t, val)
          /\ UNCHANGED <<versions, hist, histPos, stampHas, stampMap, stampReads, stampVal>>

RecordStamps ==
    /\ LET cur == versions[hist[histPos]]
           rset == QueryReadSet(cur.state)
       IN /\ stampHas' = TRUE
          /\ stampReads' = rset
          /\ stampMap' = [o \in Objects |-> IF o \in rset THEN cur.stamps[o] ELSE 0]
          /\ stampVal' = QueryEval(cur.state)
          /\ UNCHANGED <<versions, hist, histPos, memoHas, memoVer, memoReads,
                         memoVal, memoValId, pc, tTargetVer, tSnapVer,
                         tSnapReads, tSnapVal, tSnapValId, staleRead>>

Next ==
    \/ \E o \in Objects, val \in Vals: CommitEdit(o, val)
    \/ Undo
    \/ Redo
    \/ \E v \in 1..MaxVersions: ExpireWeakVersion(v)
    \/ \E t \in Threads: StartLookup(t) \/ ReuseCheck(t) \/ ReuseRelock(t) \/ FinishCompute(t)
    \/ RecordStamps

Spec == Init /\ [][Next]_vars

\* ---- Invariants ----

NoStaleRead == ~staleRead

CacheEntryCoherent ==
    (memoHas /\ versions[memoVer].alive) =>
        /\ memoVal = QueryEval(versions[memoVer].state)
        /\ memoReads = QueryReadSet(versions[memoVer].state)

StampSoundness ==
    LET cur == versions[hist[histPos]]
        allHold == \A o \in stampReads: cur.stamps[o] = stampMap[o]
    IN (stampHas /\ allHold) => (stampVal = QueryEval(cur.state))

================================================================================
