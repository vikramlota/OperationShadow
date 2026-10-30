import os
from typing import Optional
from ultron.schemas.mission import MissionPlan
from ultron.schemas.world import WorldModel
from ultron.planner.prompts import SYSTEM_PROMPT
from ultron.planner.cache import get_fallback_plan


def invoke_gemini_planner(
    world: WorldModel,
    repair_prompt: Optional[str] = None,
    trigger_rationale: Optional[str] = None
) -> MissionPlan:
    """
    Invokes the Gemini LLM to generate or repair a MissionPlan.
    Uses structured outputs to guarantee schema adherence.
    Falls back to cached plan if API is unreachable or times out.
    """
    user_prompt = f"CURRENT WORLD STATE:\n{world.model_dump_json(indent=2)}\n\n"

    if trigger_rationale:
        user_prompt += f"REPLAN TRIGGER DETECTED: {trigger_rationale}\nAdjust the mission plan accordingly.\n"

    if repair_prompt:
        user_prompt += f"VALIDATION FAILURE FEEDBACK:\n{repair_prompt}\nFix the plan.\n"

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return get_fallback_plan(world.mission.id)

    try:
        from google import genai
        client = genai.Client(api_key=api_key)

        response = client.models.generate_content(
            model='gemini-2.0-flash',
            contents=user_prompt,
            config={
                'system_instruction': SYSTEM_PROMPT,
                'response_mime_type': 'application/json',
                'response_schema': MissionPlan,
                'temperature': 0.1
            }
        )
        if hasattr(response, "parsed") and response.parsed:
            return response.parsed
        return get_fallback_plan(world.mission.id)

    except Exception as e:
        print(f"LLM Call Failed or Timed Out: {e}")
        if not trigger_rationale and not repair_prompt:
            return get_fallback_plan(world.mission.id)
        raise RuntimeError("LLM Replanning failed. Graph should route to DEGRADED.") from e
