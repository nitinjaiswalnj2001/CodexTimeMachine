"""Deterministic LCS alignment and conservative unmatched classification."""
from backend.temporal.integrity import canonical_json_bytes,sha256_bytes
from .models import DifferenceType,EventDifference,AlignmentSummary
from .normalizer import signature_key

def _id(kind,b,r): return "diff-"+sha256_bytes(canonical_json_bytes({"kind":kind,"b":[x.event_id for x in b],"r":[x.event_id for x in r]}))[:24]
def _diff(kind,b,r,summary,basis,confidence=.95):
    return EventDifference(difference_id=_id(kind,b,r),difference_type=kind,baseline_event_ids=[x.event_id for x in b],replay_event_ids=[x.event_id for x in r],baseline_sequences=[x.sequence for x in b],replay_sequences=[x.sequence for x in r],event_category=(r or b)[0].signature.event_category,summary=summary,evidence_basis=basis,confidence=confidence,limitations=["Structural signatures are deterministic but do not establish semantic equivalence."])
def _lcs(a,b):
    n,m=len(a),len(b); d=[[0]*(m+1) for _ in range(n+1)]
    for i in range(n-1,-1,-1):
        for j in range(m-1,-1,-1): d[i][j]=1+d[i+1][j+1] if signature_key(a[i])==signature_key(b[j]) else max(d[i+1][j],d[i][j+1])
    i=j=0;pairs=[]
    while i<n and j<m:
        if signature_key(a[i])==signature_key(b[j]):pairs.append((i,j));i+=1;j+=1
        elif d[i+1][j]>=d[i][j+1]:i+=1
        else:j+=1
    return pairs
def align_events(baseline,replay):
    if len(baseline)==len(replay) and baseline and [signature_key(x) for x in baseline] != [signature_key(x) for x in replay] and sorted(map(repr,[signature_key(x) for x in baseline]))==sorted(map(repr,[signature_key(x) for x in replay])):
        available=list(replay); diffs=[]
        for item in baseline:
            match=next(x for x in available if signature_key(x)==signature_key(item)); available.remove(match)
            diffs.append(_diff(DifferenceType.REORDERED,[item],[match],"Observable events occur in a different order.","The same comparison signatures are present in a different order."))
        return diffs,AlignmentSummary(baseline_event_count=len(baseline),replay_event_count=len(replay),matched_count=0,baseline_only_count=0,replay_only_count=0,modified_count=0,reordered_count=len(diffs),expanded_count=0,contracted_count=0)
    pairs=_lcs(baseline,replay);diffs=[];bi=ri=0
    for pi,pj in pairs+[(len(baseline),len(replay))]:
        bs=baseline[bi:pi];rs=replay[ri:pj]
        if bs or rs:
            bkeys=[signature_key(x) for x in bs]; rkeys=[signature_key(x) for x in rs]
            if len(bs)==len(rs) and len(bs)>0 and sorted(map(repr,bkeys))==sorted(map(repr,rkeys)):
                available=list(rs)
                for item in bs:
                    match=next(x for x in available if signature_key(x)==signature_key(item)); available.remove(match)
                    diffs.append(_diff(DifferenceType.REORDERED,[item],[match],"Observable events occur in a different order.","The same comparison signatures are present in a different unmatched alignment order."))
            elif len(bs)==1 and len(rs)==1:
                kind=DifferenceType.REORDERED if signature_key(bs[0])==signature_key(rs[0]) else DifferenceType.MODIFIED
                diffs.append(_diff(kind,bs,rs,"Observable event changed between histories.","The aligned positions share a boundary but their comparison signatures differ."))
            elif len(bs)==1 and len(rs)>1: diffs.append(_diff(DifferenceType.EXPANDED,bs,rs,"Replay contains an expanded observable sequence.","One baseline event corresponds to multiple replay events.",.8))
            elif len(bs)>1 and len(rs)==1: diffs.append(_diff(DifferenceType.CONTRACTED,bs,rs,"Replay contains a contracted observable sequence.","Multiple baseline events correspond to one replay event.",.8))
            else:
                diffs.extend(_diff(DifferenceType.BASELINE_ONLY,[x],[],"Observable event appears only in the baseline.","No matching replay signature was aligned.") for x in bs)
                diffs.extend(_diff(DifferenceType.REPLAY_ONLY,[],[x],"Observable event appears only in the replay.","No matching baseline signature was aligned.") for x in rs)
        if pi<len(baseline): diffs.append(_diff(DifferenceType.MATCHED,[baseline[pi]],[replay[pj]],"Observable event signature matched.","Deterministic comparison signatures are identical."))
        bi=pi+1;ri=pj+1
    counts={k:sum(x.difference_type is k for x in diffs) for k in DifferenceType}
    return diffs,AlignmentSummary(baseline_event_count=len(baseline),replay_event_count=len(replay),matched_count=counts[DifferenceType.MATCHED],baseline_only_count=counts[DifferenceType.BASELINE_ONLY],replay_only_count=counts[DifferenceType.REPLAY_ONLY],modified_count=counts[DifferenceType.MODIFIED],reordered_count=counts[DifferenceType.REORDERED],expanded_count=counts[DifferenceType.EXPANDED],contracted_count=counts[DifferenceType.CONTRACTED])
