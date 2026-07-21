"""Deterministic no-credit replay provider."""
from pathlib import Path
from .models import ReplayExecutionMode
from backend.temporal.integrity import canonical_json_bytes, sha256_bytes
from .models import ReplayProviderResult

class DeterministicFakeReplayProvider:
    name, version = "fake", "2.0.0"
    execution_mode = ReplayExecutionMode.DETERMINISTIC_FAKE
    def __init__(self, *, mode="success", thread_id=None, raw_thread_id=None,
                 isolation_succeeded=True, effective_overrides=None):
        self.mode,self.thread_id,self.raw_thread_id,self.execution_count=mode,thread_id,raw_thread_id,0
        self.isolation_succeeded=isolation_succeeded
        self.effective_overrides=effective_overrides or {}
        self.call_order=[]
    def execute(self,prompt:str,workspace:Path,configuration,raw_events_path:Path,final_message_path:Path,stderr_path:Path,*,preflight=None):
        self.execution_count+=1; thread=self.thread_id or f"fake-replay-{sha256_bytes(prompt.encode())[:16]}"
        self.call_order.append("provider")
        threads=[] if self.mode=="no_thread" else ([thread,thread+"-2"] if self.mode=="multiple_threads" else [thread])
        raw_threads = ([self.raw_thread_id] if self.raw_thread_id is not None else threads)
        events=[{"type":"thread.started","thread_id":x} for x in raw_threads]+[{"type":"turn.started"}]
        msg={"id":"msg-1","type":"agent_message","text":"I will evaluate a representative labeled query set covering lexical overlap, paraphrased, synonym-heavy, long-form legal, and exact-term query types before deciding on a production default."}
        tests=workspace/"tests"; tests.mkdir(parents=True,exist_ok=True)
        cases=tests/"representative_retrieval_cases.json"
        cases.write_bytes(canonical_json_bytes({"fixture":"deterministic fake replay evidence","query_categories":["lexical overlap","paraphrased","synonym-heavy","long-form legal","exact-term"],"values_are_synthetic":True,"metrics":None})+b"\n")
        test_file=tests/"test_representative_retrieval.py"
        test_file.write_text("# Deterministic fake representative-evaluation fixture.\n",encoding="utf-8")
        file={"id":"file-1","type":"file_change","status":"completed","changes":[{"path":"tests/representative_retrieval_cases.json","kind":"add"},{"path":"tests/test_representative_retrieval.py","kind":"add"}]}
        cmd={"id":"cmd-1","type":"command_execution","command":"python -m pytest -q tests/test_representative_retrieval.py","status":"completed","exit_code":0,"aggregated_output":"representative categories evaluated: lexical overlap, paraphrased, synonym-heavy, long-form legal, exact-term\n1 passed\n"}
        evidence={"id":"msg-2","type":"agent_message","text":"The representative labeled query categories were evaluated before the production-default decision."}
        final=b"I evaluated the candidate recommendation on a labeled set spanning multiple query types. The broader evidence does not justify selecting a production default from the earlier focused examples alone, so I am leaving the default unchanged pending a project-approved representative benchmark.\n"
        last={"id":"msg-3","type":"agent_message","text":final.decode().rstrip()}
        for item in (msg,file,cmd,evidence,last): events += [{"type":"item.started","item":item},{"type":"item.completed","item":item}]
        events.append({"type":"turn.completed"})
        if self.mode in {"web","mcp"}: events.insert(-1,{"type":"item.completed","item":{"id":"forbidden","type":"web_search" if self.mode=="web" else "mcp_tool_call"}})
        raw=b"".join(canonical_json_bytes(x)+b"\n" for x in events)+(b"bad-json\n" if self.mode=="invalid_json" else b"")
        if self.mode=="missing_final": final=None
        return ReplayProviderResult(exit_code=1 if self.mode in {"failure","auth_failure"} else 0,thread_ids=threads,
            raw_event_bytes=raw,final_response_bytes=final,stderr_bytes=b"fake replay stderr\n" if "failure" in self.mode else b"",provider_version=self.version)
