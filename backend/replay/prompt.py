"""Minimal replay prompt construction."""

def build_replay_prompt(task: str, clue: str) -> str:
    task, clue = task.strip(), clue.strip()
    if not task or not clue:
        raise ValueError("replay task and clue must be non-empty")
    return (f"ORIGINAL TASK\n\n{task}\n\nMINIMUM INVESTIGATIVE CLUE\n\n{clue}\n\n"
            "Treat the clue as an additional investigative constraint, not as a revealed answer.\n")
