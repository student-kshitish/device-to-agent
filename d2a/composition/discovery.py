from __future__ import annotations
from typing import Any


class Discovery:
    def find_providers(
        self,
        role_specs: list[dict],
        capability_pool: list[dict],
    ) -> dict[int, list[dict]]:
        """
        Match each role-spec to all candidates in capability_pool.
        Returns dict: role_index -> list of candidate dicts.

        Each candidate in pool must have:
          {node_id, capability, contract (IOContract), device_class, live_state, role}
        """
        result: dict[int, list[dict]] = {}
        for i, spec in enumerate(role_specs):
            role = spec["role"]
            media = spec.get("media")
            matches = []
            for entry in capability_pool:
                if entry.get("role") != role:
                    continue
                contract = entry.get("contract")
                # media filter: if spec requires a media type, check producer contract
                if media and contract and contract.media != media:
                    continue
                matches.append(entry)
            result[i] = matches
        return result
