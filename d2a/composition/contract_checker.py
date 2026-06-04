from __future__ import annotations
from typing import Tuple
from d2a.contracts import IOContract, contracts_compatible

# Media types that can be converted via adapters
_CONVERTIBLE_PAIRS = {
    ("image", "image"),   # jpeg <-> raw_rgb, resize, colorspace
    ("image", "tensor"),  # raw_rgb -> float32 tensor
}


class ContractChecker:
    def check(
        self,
        producer_candidate: dict,
        consumer_candidate: dict,
    ) -> Tuple[str, str]:
        """
        Check if producer can feed consumer.
        Returns (status, reason):
          status: "exact" | "needs_adapter" | "incompatible"
        """
        prod_contract: IOContract | None = producer_candidate.get("contract")
        cons_contract: IOContract | None = consumer_candidate.get("contract")

        if prod_contract is None or cons_contract is None:
            return "incompatible", "missing contract on producer or consumer"

        ok, reason = contracts_compatible(prod_contract, cons_contract)
        if ok:
            return "exact", reason

        # Not exact — check if media pair is convertible via adapters
        pair = (prod_contract.media, cons_contract.media)
        if pair in _CONVERTIBLE_PAIRS:
            return "needs_adapter", reason

        return "incompatible", (
            f"fundamental incompatibility: {prod_contract.media} → {cons_contract.media} "
            f"has no adapter path. Detail: {reason}"
        )
