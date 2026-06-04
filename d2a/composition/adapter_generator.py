from __future__ import annotations
from typing import List, Optional, Tuple
from d2a.contracts import IOContract
from d2a.adapters import find_adapter_chain


class AdapterGenerator:
    def build(
        self,
        producer_out: IOContract,
        consumer_in: IOContract,
    ) -> Tuple[Optional[List], float]:
        """
        Find adapter chain from producer_out to consumer_in.
        Returns (chain, cost): chain=[] and cost=0 if exact, (None, inf) if impossible.
        """
        from d2a.contracts import contracts_compatible
        ok, _ = contracts_compatible(producer_out, consumer_in)
        if ok:
            return [], 0.0

        chain, cost = find_adapter_chain(producer_out, consumer_in)
        return chain, cost

    def describe_chain(self, chain: Optional[List]) -> str:
        if chain is None:
            return "<no adapter path>"
        if not chain:
            return "<exact match, no adapters needed>"
        return " → ".join(a.describe() for a in chain)

    def final_contract(
        self, producer_out: IOContract, chain: Optional[List]
    ) -> Optional[IOContract]:
        """Simulate chain application to get final output contract."""
        if chain is None:
            return None
        current = producer_out
        for adapter in chain:
            current = adapter.produces_for(current)
        return current
