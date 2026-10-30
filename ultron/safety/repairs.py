from typing import List

def build_repair_prompt(failed_plan_rationale: str, errors: List[str]) -> str:
    """
    Constructs a deterministic feedback string forcing the LLM to correct its mistakes.
    """
    error_bullet_points = "\n".join([f"- {err}" for err in errors])
    
    return f"""
    Your previous plan (Rationale: "{failed_plan_rationale}") was REJECTED by the Safety Validator.
    
    You violated the following strict constraints:
    {error_bullet_points}
    
    You must generate a new create_mission_plan tool call that resolves these errors. 
    Remember: Do not invent areas or targets not present in the provided world state, 
    and never assign unconstrained navigation to HAWK01.
    """
    