import os
from typing import Optional
from pathlib import Path
from ultron.schemas.mission import MissionPlan
from ultron.schemas.world import WorldModel
from ultron.planner.prompts import SYSTEM_PROMPT
from ultron.planner.cache import get_fallback_plan

# Automatically load .env if available
try:
    import dotenv
    root_dir = Path(__file__).resolve().parent.parent.parent
    for env_path in [root_dir / ".env", Path(".env")]:
        if env_path.is_file():
            dotenv.load_dotenv(env_path, override=True)
            break
except ImportError:
    pass

# Default timeouts — overridden by UltronConfig when available
_PLANNING_TIMEOUT_SEC: float = 30.0
_REPLANNING_TIMEOUT_SEC: float = 20.0

try:
    from ultron.config import config as _ultron_cfg
    _PLANNING_TIMEOUT_SEC = _ultron_cfg.planning_timeout_sec
    _REPLANNING_TIMEOUT_SEC = _ultron_cfg.replanning_timeout_sec
except Exception:
    pass


def invoke_gemini_planner(
    world: WorldModel,
    repair_prompt: Optional[str] = None,
    trigger_rationale: Optional[str] = None,
    language: str = "English",
) -> MissionPlan:
    """
    Invokes the Gemini LLM to generate or repair a MissionPlan.
    Uses structured outputs (response_schema) to guarantee schema adherence.
    AFC is explicitly disabled to avoid the SDK's automatic-function-calling
    path, which is triggered when a Pydantic class is passed as response_schema.
    Falls back to the cached plan if the API is unreachable or times out.
    """
    user_prompt = f"CURRENT WORLD STATE:\n{world.model_dump_json(indent=2)}\n\n"
    user_prompt += f"RATIONALE_LANGUAGE: {language}\n\n"

    if trigger_rationale:
        user_prompt += (
            f"REPLAN TRIGGER DETECTED: {trigger_rationale}\n"
            "Adjust the mission plan accordingly.\n"
        )

    if repair_prompt:
        user_prompt += (
            f"VALIDATION FAILURE FEEDBACK:\n{repair_prompt}\nFix the plan.\n"
        )

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return get_fallback_plan(world.mission.id)

    model_name = os.getenv("SHADOW_LLM_MODEL", "gemini-2.5-flash")

    # Use a longer timeout for initial planning vs. replanning
    timeout_sec = _REPLANNING_TIMEOUT_SEC if trigger_rationale else _PLANNING_TIMEOUT_SEC

    try:
        from google import genai
        from google.genai import types as genai_types

        client = genai.Client(
            api_key=api_key,
            http_options={"timeout": int(timeout_sec * 1000)},
        )

        response = client.models.generate_content(
            model=model_name,
            contents=user_prompt,
            config=genai_types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                response_mime_type="application/json",
                response_schema=MissionPlan,
                temperature=0.1,
                # Disable SDK-level automatic function calling (AFC).
                # Passing a Pydantic class as response_schema triggers AFC
                # detection in the SDK even when no tools are defined, which
                # emits a noisy warning. We only need structured JSON output.
                automatic_function_calling=genai_types.AutomaticFunctionCallingConfig(
                    disable=True
                ),
            ),
        )
        if hasattr(response, "parsed") and response.parsed:
            return MissionPlan.model_validate(response.parsed)
        return get_fallback_plan(world.mission.id)

    except Exception as e:
        print(f"LLM Call Failed or Timed Out ({model_name}): {e}")
        return get_fallback_plan(world.mission.id)
