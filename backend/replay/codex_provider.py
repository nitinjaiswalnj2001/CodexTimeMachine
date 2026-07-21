"""Real fresh Codex replay provider; excluded from automated acceptance."""
from pathlib import Path
from backend.runs.codex_cli import CodexCLIAdapter
from backend.runs.events import summarize_event_stream
from .models import ReplayProviderResult
from .models import ReplayExecutionMode
class CodexReplayProvider:
    name,version="codex","local"
    execution_mode = ReplayExecutionMode.LIVE_MODEL
    def __init__(self,executable="codex"): self.adapter=CodexCLIAdapter(executable)
    def execute(self,prompt,workspace:Path,configuration,raw_events_path:Path,final_message_path:Path,stderr_path:Path,*,preflight=None):
        if configuration is None:
            raise ValueError("live Codex replay requires execution configuration")
        if preflight is None:
            raise ValueError("validated replay preflight is required")
        pf=preflight
        result=self.adapter.execute(self.adapter.build_command(pf,configuration,workspace,final_message_path,prompt),workspace,raw_events_path,stderr_path,configuration.timeout_seconds)
        summary=summarize_event_stream(raw_events_path)
        return ReplayProviderResult(exit_code=result.exit_code if result.exit_code is not None else 1,thread_ids=[summary.thread_id] if summary.thread_id else [],raw_event_bytes=raw_events_path.read_bytes(),final_response_bytes=final_message_path.read_bytes() if final_message_path.exists() else None,stderr_bytes=stderr_path.read_bytes(),provider_version=pf.version or "unavailable",timing_metadata={"timed_out":result.timed_out})
