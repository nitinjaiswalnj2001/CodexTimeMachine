"""Conservative rules for observable divergence evidence only."""
from collections import Counter
from .models import (BehavioralDimension,BehavioralDimensionAssessment,DimensionStatus,
 DifferenceType,FirstDivergence,MessageAction,ObservableOutcome,OutcomeStatus,EventCategory)

LIMIT="Lexical and structural rules describe emitted evidence only and may miss semantic equivalence."
def _ids(events,predicate): return [x.event_id for x in events if predicate(x)]
def _first(difference, summary=None):
    if difference is None:return None
    return FirstDivergence(baseline_sequence=difference.baseline_sequences[0] if difference.baseline_sequences else None,replay_sequence=difference.replay_sequences[0] if difference.replay_sequences else None,baseline_event_id=difference.baseline_event_ids[0] if difference.baseline_event_ids else None,replay_event_id=difference.replay_event_ids[0] if difference.replay_event_ids else None,divergence_type=difference.difference_type,summary=summary or difference.summary,evidence_basis=difference.evidence_basis)
def first_structural_divergence(differences):
    return _first(next((x for x in differences if x.difference_type is not DifferenceType.MATCHED),None))
def _actions(event): return set(event.signature.message_actions or ([event.signature.message_action] if event.signature.message_action else []))
def _evaluation(event): return MessageAction.EVALUATION_INTENT in _actions(event) or "evaluation" in event.signature.command_category or "test_execution" in event.signature.command_category
def _evidence(event): return (_evaluation(event) or MessageAction.EVIDENCE_SUMMARY in _actions(event) or (event.signature.event_category is EventCategory.COMMAND and bool(event.signature.command_category)))
def _investigation(event): return bool(_actions(event) & {MessageAction.EVALUATION_INTENT,MessageAction.INVESTIGATION_ANNOUNCEMENT}) or _evidence(event)
def first_investigative_divergence(differences,baseline,replay):
    changed=[x for x in differences if x.difference_type is not DifferenceType.MATCHED]
    for difference in changed:
        events=[x for x in baseline if x.event_id in difference.baseline_event_ids]+[x for x in replay if x.event_id in difference.replay_event_ids]
        if any(_investigation(x) for x in events):
            replay_evaluation=any(x.event_id in difference.replay_event_ids and _evaluation(x) for x in replay)
            return _first(difference,"The replay announced broader evaluation before making a recommendation." if replay_evaluation else "An observable investigative action differs between histories.")
    return None
def first_replay_evaluation_divergence(differences,replay):
    for difference in differences:
        if difference.difference_type is DifferenceType.MATCHED:continue
        if any(event.event_id in difference.replay_event_ids and _evaluation(event) for event in replay):
            return _first(difference,"The replay announced or executed an observable evaluation action.")
    return None
def _profile_status(baseline, replay):
    """Compare profiles, never their source-event citation IDs."""
    baseline=set(baseline); replay=set(replay)
    if not baseline and not replay:return DimensionStatus.INSUFFICIENT_EVIDENCE
    if baseline==replay:return DimensionStatus.UNCHANGED
    if replay > baseline:return DimensionStatus.MORE_OBSERVED_IN_REPLAY
    if baseline > replay:return DimensionStatus.LESS_OBSERVED_IN_REPLAY
    return DimensionStatus.DIFFERENT
def _multiset_status(baseline, replay):
    """Compare multiplicity-sensitive observable activity without event IDs."""
    baseline, replay = Counter(baseline), Counter(replay)
    if not baseline and not replay:return DimensionStatus.INSUFFICIENT_EVIDENCE
    if baseline==replay:return DimensionStatus.UNCHANGED
    if all(replay[key]>=count for key,count in baseline.items()) and any(replay[key]>baseline[key] for key in replay):return DimensionStatus.MORE_OBSERVED_IN_REPLAY
    if all(baseline[key]>=count for key,count in replay.items()) and any(baseline[key]>replay[key] for key in baseline):return DimensionStatus.LESS_OBSERVED_IN_REPLAY
    return DimensionStatus.DIFFERENT
def _assessment(dimension,bids,rids,summary,status=None,baseline_profile=(),replay_profile=()):
    if status is None: status=_profile_status(baseline_profile,replay_profile)
    return BehavioralDimensionAssessment(dimension=dimension,status=status,baseline_evidence=bids,replay_evidence=rids,summary=summary,confidence=.85,limitations=[LIMIT])
def _evaluation_profile(events):
    result=set()
    for event in events:
        text=(event.summary+" "+(event.signature.normalized_command or "")).casefold()
        if MessageAction.EVALUATION_INTENT in _actions(event): result.add("EVALUATION_INTENT")
        for tag in event.signature.command_category:
            if tag in {"evaluation","test_execution","compilation"}: result.add(tag)
        if event.signature.event_category in {EventCategory.MESSAGE,EventCategory.COMMAND} and any(term in text for term in ("benchmark","representative","query set","query types")): result.add("representative_query_set")
        if event.signature.command_executable in {"pytest","unittest"} or any(token in (event.signature.normalized_command or "").casefold() for token in ("pytest","unittest")): result.add("test_runner")
    return result
def _evidence_profile(events):
    result=[]
    for event in events:
        if event.signature.event_category is EventCategory.COMMAND:
            for tag in event.signature.command_category:
                if tag in {"test_execution","evaluation","file_inspection","repository_inspection"}:result.append(f"command:{tag}")
            if event.signature.command_executable in {"pytest","unittest"} or any(token in (event.signature.normalized_command or "").casefold() for token in ("pytest","unittest")):result.append("command:test_runner")
        if MessageAction.EVIDENCE_SUMMARY in _actions(event):result.append("evidence_summary")
    return Counter(result)
def _file_profile(events):
    return Counter((event.signature.file_operation, tuple(event.signature.paths)) for event in events if event.signature.event_category is EventCategory.FILE)
def _command_profile(events):
    return Counter((tuple(event.signature.command_category),event.signature.command_executable,event.signature.normalized_command) for event in events if event.signature.event_category is EventCategory.COMMAND)
def _investigative_profile(events):
    result=[]
    for event in events:
        message_actions=_actions(event) & {MessageAction.INVESTIGATION_ANNOUNCEMENT,MessageAction.EVALUATION_INTENT,MessageAction.EVIDENCE_SUMMARY}
        if message_actions:result.extend(f"message:{action.value}" for action in sorted(message_actions,key=lambda action:action.value))
        elif event.signature.event_category is EventCategory.COMMAND and _evidence(event):result.append("command:"+",".join(event.signature.command_category or [event.signature.command_executable or "other"]))
    return tuple(result)
def _recommendation_profile(events):
    result=set()
    for event in events:
        if MessageAction.RECOMMENDATION not in _actions(event):continue
        text=event.summary.casefold()
        if "production default" in text or "default" in text:result.add("production_default")
        if any(word in text for word in ("if ","unless ","depending ","conditional")):result.add("conditional")
        if any(word in text for word in ("further evaluation","more evaluation","evaluate before","verify before")):result.add("request_further_evaluation")
        if any(word in text for word in ("provisional","qualified","tentative")):result.add("qualified")
    return result
def _before_recommendation(events):
    recommendation=next((x.sequence for x in events if MessageAction.RECOMMENDATION in _actions(x)),None)
    if recommendation is None:return None
    return any(_evaluation(x) and x.sequence<recommendation for x in events)
def dimensions(b,r):
    b_eval=_ids(b,lambda x:bool(_evaluation_profile([x])));r_eval=_ids(r,lambda x:bool(_evaluation_profile([x])))
    b_evidence=_ids(b,lambda x:bool(_evidence_profile([x])));r_evidence=_ids(r,lambda x:bool(_evidence_profile([x])))
    b_files=_ids(b,lambda x:x.signature.event_category is EventCategory.FILE);r_files=_ids(r,lambda x:x.signature.event_category is EventCategory.FILE)
    b_commands=_ids(b,lambda x:x.signature.event_category is EventCategory.COMMAND);r_commands=_ids(r,lambda x:x.signature.event_category is EventCategory.COMMAND)
    b_rec=_ids(b,lambda x:MessageAction.RECOMMENDATION in _actions(x));r_rec=_ids(r,lambda x:MessageAction.RECOMMENDATION in _actions(x))
    b_actions=_investigative_profile(b);r_actions=_investigative_profile(r)
    timing_b=_before_recommendation(b);timing_r=_before_recommendation(r)
    timing_status=DimensionStatus.INSUFFICIENT_EVIDENCE if timing_b is None or timing_r is None else (DimensionStatus.UNCHANGED if timing_b==timing_r else DimensionStatus.DIFFERENT)
    b_scope=_recommendation_profile(b);r_scope=_recommendation_profile(r)
    scope_status=DimensionStatus.INSUFFICIENT_EVIDENCE if not b_rec or not r_rec or not b_scope or not r_scope else _profile_status(b_scope,r_scope)
    sequence_status=DimensionStatus.INSUFFICIENT_EVIDENCE if not b_actions and not r_actions else (DimensionStatus.UNCHANGED if b_actions==r_actions else DimensionStatus.DIFFERENT)
    return [_assessment(BehavioralDimension.EVALUATION_BREADTH,b_eval,r_eval,"Distinct observable evaluation dimensions were compared; repeated instances do not by themselves broaden evaluation breadth.",baseline_profile=_evaluation_profile(b),replay_profile=_evaluation_profile(r)),_assessment(BehavioralDimension.EVIDENCE_GATHERING,b_evidence,r_evidence,"Multiplicity-sensitive observable evidence commands, summaries, result verification, and evaluation-related file activity were compared.",_multiset_status(_evidence_profile(b),_evidence_profile(r))),_assessment(BehavioralDimension.DECISION_TIMING,b_rec,r_rec,"Ordering of observable evaluation relative to recommendation messages was compared.",timing_status),_assessment(BehavioralDimension.FILE_ACTIVITY,b_files,r_files,"Multiplicity-sensitive normalized file-operation and workspace-relative path profiles were compared.",_multiset_status(_file_profile(b),_file_profile(r))),_assessment(BehavioralDimension.COMMAND_ACTIVITY,b_commands,r_commands,"Multiplicity-sensitive normalized command categories, executables, and portable command forms were compared.",_multiset_status(_command_profile(b),_command_profile(r))),_assessment(BehavioralDimension.RECOMMENDATION_SCOPE,b_rec,r_rec,"Recommendation scope is reported only where emitted message text contains conservative scope markers.",scope_status,baseline_profile=b_scope,replay_profile=r_scope),_assessment(BehavioralDimension.INVESTIGATIVE_SEQUENCE,_ids(b,_investigation),_ids(r,_investigation),"Ordered observable investigation, evaluation, and evidence-gathering actions were compared.",sequence_status,baseline_profile=b_actions,replay_profile=r_actions)]
def validate_outcome_consistency(value, dimension_values):
    statuses={item.dimension: item.status for item in dimension_values}
    statement=value.statement.casefold()
    evaluation_more=any(statuses.get(dimension) is DimensionStatus.MORE_OBSERVED_IN_REPLAY for dimension in (BehavioralDimension.EVALUATION_BREADTH,BehavioralDimension.EVIDENCE_GATHERING))
    if "additional observable evaluation" in statement and not evaluation_more: raise ValueError("outcome claims additional evaluation without a MORE_OBSERVED_IN_REPLAY dimension")
    if "recommendation" in statement and not any(item.dimension is BehavioralDimension.DECISION_TIMING and item.status is not DimensionStatus.INSUFFICIENT_EVIDENCE for item in dimension_values):
        raise ValueError("outcome mentions recommendation without grounded decision timing")
def outcome(differences,baseline,replay,dimension_values=None):
    dimension_values=dimension_values if dimension_values is not None else dimensions(baseline,replay)
    changed=[x for x in differences if x.difference_type is not DifferenceType.MATCHED]
    if not changed:
        status=OutcomeStatus.NO_OBSERVABLE_CHANGE_DETECTED;statement="No structural change was detected in the observable event histories.";support=[]
    else:
        changed_replay={i for x in changed for i in x.replay_event_ids};evaluation=[x for x in replay if x.event_id in changed_replay and _evaluation(x)]
        statuses={item.dimension:item.status for item in dimension_values}
        breadth=statuses[BehavioralDimension.EVALUATION_BREADTH]; evidence=statuses[BehavioralDimension.EVIDENCE_GATHERING]
        status=OutcomeStatus.OBSERVABLE_CHANGE_DETECTED;support=changed
        if breadth is DimensionStatus.MORE_OBSERVED_IN_REPLAY or evidence is DimensionStatus.MORE_OBSERVED_IN_REPLAY:
            timing=statuses.get(BehavioralDimension.DECISION_TIMING)
            statement="The replay exhibited additional observable evaluation behavior before completing its recommendation." if timing is not DimensionStatus.INSUFFICIENT_EVIDENCE else "The replay exhibited additional observable evaluation behavior."
        elif evaluation and (breadth is DimensionStatus.LESS_OBSERVED_IN_REPLAY or evidence is DimensionStatus.LESS_OBSERVED_IN_REPLAY):
            statement="The histories differ observably. The replay introduced a distinct evaluation announcement, while the baseline contained more observed evaluation and evidence-gathering activity overall."
        else: statement="The baseline and replay contain different observable event histories."
    value=ObservableOutcome(status=status,statement=statement,supporting_difference_ids=[x.difference_id for x in support],supporting_baseline_event_ids=sorted({i for x in support for i in x.baseline_event_ids}),supporting_replay_event_ids=sorted({i for x in support for i in x.replay_event_ids}),confidence=.9 if changed else .8,limitations=["The comparison does not judge technical correctness or performance.","The replay differs from the baseline after receiving the approved clue, but this comparison alone does not prove that the clue caused every observed difference."])
    validate_outcome_consistency(value,dimension_values)
    return value
