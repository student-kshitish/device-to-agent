from __future__ import annotations
from dataclasses import dataclass, field
from typing import List, Optional, Any
from itertools import product as iterproduct

from d2a.contracts import IOContract


@dataclass
class HopRecord:
    role_index: int
    node_id: str
    capability_name: str
    role: str
    contract_in: Optional[IOContract]
    adapter_chain: List[Any]
    contract_out: Optional[IOContract]
    score: float
    adapter_cost: float

    def describe(self) -> str:
        chain_str = (
            " → ".join(a.describe() for a in self.adapter_chain)
            if self.adapter_chain else "exact"
        )
        return (
            f"  [{self.role_index}] {self.role} | node={self.node_id[:12]} "
            f"cap={self.capability_name}\n"
            f"      in:  {self.contract_in}\n"
            f"      chain: {chain_str}\n"
            f"      out: {self.contract_out}\n"
            f"      score={self.score:.4f}  adapter_cost={self.adapter_cost:.2f}"
        )


@dataclass
class Blueprint:
    goal: str
    hops: List[HopRecord]
    total_cost: float
    valid: bool
    reject_reason: str = ""

    def describe(self) -> str:
        lines = [f"Blueprint(goal={self.goal}, total_cost={self.total_cost:.3f}, valid={self.valid})"]
        for hop in self.hops:
            lines.append(hop.describe())
        if not self.valid:
            lines.append(f"  REJECTED: {self.reject_reason}")
        return "\n".join(lines)

    def provider_ids(self) -> list[str]:
        return [h.node_id for h in self.hops]


class CostEvaluator:
    INCOMPATIBLE_PENALTY = 1e9

    def enumerate_blueprints(
        self,
        ranked_candidates: dict[int, list[dict]],
        checker,
        adapter_gen,
        goal: str = "",
    ) -> list[Blueprint]:
        """
        Enumerate all pairwise combinations of candidates across roles.
        Skip incompatible pairs; include needs_adapter pairs with their chain.
        Returns list of Blueprint objects (valid and invalid for transparency).
        """
        role_indices = sorted(ranked_candidates.keys())
        if not role_indices:
            return []

        # Build candidate lists per role
        candidate_lists = [ranked_candidates[i] for i in role_indices]

        blueprints = []
        for combo in iterproduct(*candidate_lists):
            # combo is a tuple: one candidate per role in order
            hops = []
            total_cost = 0.0
            valid = True
            reject_reason = ""

            for idx, cand in zip(role_indices, combo):
                score = cand.get("_score", 0.0)
                # Cost contribution: invert score (lower score = higher cost)
                provider_cost = 1.0 - score
                total_cost += provider_cost

                contract_in = cand.get("contract")
                adapter_chain = []
                adapter_cost = 0.0
                contract_out = contract_in

                # Check contract between consecutive hops
                if hops:
                    prev_hop = hops[-1]
                    prev_out = prev_hop.contract_out
                    status, reason = checker.check(
                        {"contract": prev_out},
                        {"contract": contract_in},
                    )
                    if status == "incompatible":
                        valid = False
                        reject_reason = (
                            f"hop {idx-1}→{idx}: {reason}"
                        )
                        break
                    elif status == "needs_adapter":
                        chain, ac = adapter_gen.build(prev_out, contract_in)
                        if chain is None:
                            valid = False
                            reject_reason = (
                                f"hop {idx-1}→{idx}: needs_adapter but no chain found. {reason}"
                            )
                            break
                        adapter_chain = chain
                        adapter_cost = ac
                        total_cost += ac
                        # update prev hop's contract_out to reflect adapter output
                        contract_out_after_chain = adapter_gen.final_contract(prev_out, chain)
                        hops[-1] = HopRecord(
                            role_index=hops[-1].role_index,
                            node_id=hops[-1].node_id,
                            capability_name=hops[-1].capability_name,
                            role=hops[-1].role,
                            contract_in=hops[-1].contract_in,
                            adapter_chain=chain,
                            contract_out=contract_out_after_chain,
                            score=hops[-1].score,
                            adapter_cost=ac,
                        )

                hops.append(HopRecord(
                    role_index=idx,
                    node_id=cand.get("node_id", "?"),
                    capability_name=cand.get("capability", cand.get("node_id", "?")),
                    role=cand.get("role", "?"),
                    contract_in=contract_in,
                    adapter_chain=[],
                    contract_out=contract_out,
                    score=score,
                    adapter_cost=0.0,
                ))

            blueprints.append(Blueprint(
                goal=goal,
                hops=hops,
                total_cost=round(total_cost, 4),
                valid=valid,
                reject_reason=reject_reason,
            ))

        return blueprints

    def evaluate(self, blueprint: Blueprint) -> float:
        return blueprint.total_cost

    def best(self, blueprints: list[Blueprint]) -> Optional[Blueprint]:
        valid = [b for b in blueprints if b.valid]
        if not valid:
            return None
        return min(valid, key=lambda b: b.total_cost)
