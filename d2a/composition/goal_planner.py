from __future__ import annotations
from d2a.contracts import IOContract

# Goal definitions: goal_name -> ordered list of role-specs.
# Each role-spec: {"role": str, "media": str (optional), "contract": IOContract (optional)}
GOAL_REGISTRY: dict[str, list[dict]] = {
    "vision": [
        {
            "role": "producer",
            "media": "image",
            "label": "camera",
        },
        {
            "role": "consumer",
            "label": "model",
            "contract": IOContract(media="tensor", format="float32", shape=(640, 480, 3)),
        },
    ],
    "listen": [
        {
            "role": "producer",
            "media": "audio",
            "label": "microphone",
        },
        {
            "role": "consumer",
            "label": "audio_model",
            "contract": IOContract(media="tensor", format="float32"),
        },
    ],
    "monitor": [
        {
            "role": "producer",
            "media": "scalar",
            "label": "sensor",
        },
        {
            "role": "consumer",
            "label": "monitor_sink",
        },
    ],
    "stream_audio": [
        {
            "role": "producer",
            "media": "audio",
            "label": "microphone",
        },
        {
            "role": "consumer",
            "label": "audio_sink",
            "contract": IOContract(media="audio", format="pcm16"),
        },
    ],
}


class GoalPlanner:
    def plan_requirements(self, goal: str) -> list[dict]:
        """Return ordered list of role-specs for the given goal."""
        if goal not in GOAL_REGISTRY:
            known = list(GOAL_REGISTRY.keys())
            raise ValueError(
                f"Unknown goal '{goal}'. Known goals: {known}"
            )
        return list(GOAL_REGISTRY[goal])
