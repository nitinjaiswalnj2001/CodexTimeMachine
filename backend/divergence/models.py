"""Validated Phase 8 observable-history divergence models."""
from __future__ import annotations
from datetime import datetime
from enum import StrEnum
from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "1.1"
POLICY_VERSION = "1.1.0"
class StrictModel(BaseModel): model_config = ConfigDict(extra="forbid")
class DifferenceType(StrEnum):
    MATCHED="MATCHED"; BASELINE_ONLY="BASELINE_ONLY"; REPLAY_ONLY="REPLAY_ONLY"; MODIFIED="MODIFIED"; REORDERED="REORDERED"; EXPANDED="EXPANDED"; CONTRACTED="CONTRACTED"
class EventCategory(StrEnum): THREAD="THREAD"; TURN="TURN"; MESSAGE="MESSAGE"; COMMAND="COMMAND"; FILE="FILE"
class MessageAction(StrEnum):
    INVESTIGATION_ANNOUNCEMENT="INVESTIGATION_ANNOUNCEMENT"; EVALUATION_INTENT="EVALUATION_INTENT"; RECOMMENDATION="RECOMMENDATION"; IMPLEMENTATION_INTENT="IMPLEMENTATION_INTENT"; EVIDENCE_SUMMARY="EVIDENCE_SUMMARY"; COMPLETION_STATEMENT="COMPLETION_STATEMENT"; OTHER="OTHER"
class BehavioralDimension(StrEnum):
    EVALUATION_BREADTH="EVALUATION_BREADTH"; EVIDENCE_GATHERING="EVIDENCE_GATHERING"; DECISION_TIMING="DECISION_TIMING"; FILE_ACTIVITY="FILE_ACTIVITY"; COMMAND_ACTIVITY="COMMAND_ACTIVITY"; RECOMMENDATION_SCOPE="RECOMMENDATION_SCOPE"; INVESTIGATIVE_SEQUENCE="INVESTIGATIVE_SEQUENCE"
class DimensionStatus(StrEnum):
    UNCHANGED="UNCHANGED"; MORE_OBSERVED_IN_REPLAY="MORE_OBSERVED_IN_REPLAY"; LESS_OBSERVED_IN_REPLAY="LESS_OBSERVED_IN_REPLAY"; DIFFERENT="DIFFERENT"; INSUFFICIENT_EVIDENCE="INSUFFICIENT_EVIDENCE"
class OutcomeStatus(StrEnum):
    OBSERVABLE_CHANGE_DETECTED="OBSERVABLE_CHANGE_DETECTED"; NO_OBSERVABLE_CHANGE_DETECTED="NO_OBSERVABLE_CHANGE_DETECTED"; INSUFFICIENT_EVIDENCE="INSUFFICIENT_EVIDENCE"
class ComparisonSignature(StrictModel):
    event_type:str; event_category:EventCategory; status:str; command_category:list[str]=Field(default_factory=list); command_executable:str|None=None; normalized_command:str|None=None; paths:list[str]=Field(default_factory=list); file_operation:str|None=None; message_action:MessageAction|None=None; message_actions:list[MessageAction]=Field(default_factory=list)
class ComparisonEvent(StrictModel): event_id:str; sequence:int; signature:ComparisonSignature; summary:str
class EventDifference(StrictModel):
    difference_id:str=Field(pattern=r"^diff-[0-9a-f]{24}$"); difference_type:DifferenceType; baseline_event_ids:list[str]; replay_event_ids:list[str]; baseline_sequences:list[int]; replay_sequences:list[int]; event_category:EventCategory; summary:str; evidence_basis:str; confidence:float=Field(ge=0,le=1); limitations:list[str]
class AlignmentSummary(StrictModel):
    policy_version:str=POLICY_VERSION; baseline_event_count:int=Field(ge=0); replay_event_count:int=Field(ge=0); matched_count:int=Field(ge=0); baseline_only_count:int=Field(ge=0); replay_only_count:int=Field(ge=0); modified_count:int=Field(ge=0); reordered_count:int=Field(ge=0); expanded_count:int=Field(ge=0); contracted_count:int=Field(ge=0)
class FirstDivergence(StrictModel):
    baseline_sequence:int|None=None; replay_sequence:int|None=None; baseline_event_id:str|None=None; replay_event_id:str|None=None; divergence_type:DifferenceType; summary:str; evidence_basis:str
class BehavioralDimensionAssessment(StrictModel):
    dimension:BehavioralDimension; status:DimensionStatus; baseline_evidence:list[str]; replay_evidence:list[str]; summary:str; confidence:float=Field(ge=0,le=1); limitations:list[str]
class ObservableOutcome(StrictModel):
    status:OutcomeStatus; statement:str; supporting_difference_ids:list[str]; supporting_baseline_event_ids:list[str]; supporting_replay_event_ids:list[str]; confidence:float=Field(ge=0,le=1); limitations:list[str]
class ObservableHistoryDivergence(StrictModel):
    schema_version:str=SCHEMA_VERSION; divergence_id:str=Field(pattern=r"^div-[0-9a-f]{24}$"); baseline_run_id:str; replay_id:str; scenario_id:str; base_snapshot_hash:str; baseline_trajectory_hash:str; replay_trajectory_hash:str; intervention_id:str; comparison_policy_version:str=POLICY_VERSION; normalized_baseline_events:list[ComparisonEvent]; normalized_replay_events:list[ComparisonEvent]; alignment:AlignmentSummary; first_structural_divergence:FirstDivergence|None; first_investigative_divergence:FirstDivergence|None; first_replay_evaluation_divergence:FirstDivergence|None; event_differences:list[EventDifference]; behavioral_dimensions:list[BehavioralDimensionAssessment]; observable_outcome:ObservableOutcome; limitations:list[str]; warnings:list[str]; created_at:datetime; divergence_hash:str
class DivergenceManifest(StrictModel):
    schema_version:str=SCHEMA_VERSION; divergence_id:str; baseline_run_id:str; replay_id:str; scenario_id:str; base_snapshot_hash:str; baseline_trajectory_hash:str; replay_trajectory_hash:str; intervention_id:str; divergence_hash:str; replay_directory_relative_path:str|None=None; baseline_event_count:int; replay_event_count:int; matched_count:int; baseline_only_count:int; replay_only_count:int; modified_count:int; reordered_count:int; expanded_count:int; contracted_count:int; warning_count:int; input_file_hashes:dict[str,str]; output_file_hashes:dict[str,str]; created_at:datetime
