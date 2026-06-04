from __future__ import annotations
from typing import List, Optional, Tuple
from d2a.contracts import IOContract


class ResizeAdapter:
    """Resize spatial dimensions of an image/tensor without changing format."""
    def __init__(self, target_shape: tuple):
        self.target_shape = target_shape

    def accepts(self, c: IOContract) -> bool:
        return c.media in ("image", "tensor") and c.format in ("raw_rgb", "float32")

    def produces_for(self, inp: IOContract) -> IOContract:
        return IOContract(
            media=inp.media, format=inp.format,
            shape=self.target_shape, rate=inp.rate, extra=inp.extra
        )

    def cost(self) -> float:
        return 1.0

    def describe(self) -> str:
        return f"ResizeAdapter(→{self.target_shape})"


class FormatDecodeAdapter:
    """Decode jpeg → raw_rgb."""
    def accepts(self, c: IOContract) -> bool:
        return c.media == "image" and c.format == "jpeg"

    def produces_for(self, inp: IOContract) -> IOContract:
        return IOContract(
            media="image", format="raw_rgb",
            shape=inp.shape, rate=inp.rate, extra=inp.extra
        )

    def cost(self) -> float:
        return 2.0

    def describe(self) -> str:
        return "FormatDecodeAdapter(jpeg→raw_rgb)"


class ColorspaceAdapter:
    """Convert between rgb/bgr/gray colorspaces (raw_rgb only)."""
    def __init__(self, target: str = "rgb"):
        self.target = target

    def accepts(self, c: IOContract) -> bool:
        return c.media == "image" and c.format == "raw_rgb"

    def produces_for(self, inp: IOContract) -> IOContract:
        extra = dict(inp.extra)
        extra["colorspace"] = self.target
        return IOContract(
            media="image", format="raw_rgb",
            shape=inp.shape, rate=inp.rate, extra=extra
        )

    def cost(self) -> float:
        return 0.5

    def describe(self) -> str:
        return f"ColorspaceAdapter(→{self.target})"


class RateThrottleAdapter:
    """Throttle rate to target fps/hz."""
    def __init__(self, target_rate: float):
        self.target_rate = target_rate

    def accepts(self, c: IOContract) -> bool:
        return c.rate is not None and c.rate > self.target_rate

    def produces_for(self, inp: IOContract) -> IOContract:
        return IOContract(
            media=inp.media, format=inp.format,
            shape=inp.shape, rate=self.target_rate, extra=inp.extra
        )

    def cost(self) -> float:
        return 0.2

    def describe(self) -> str:
        return f"RateThrottleAdapter(→{self.target_rate}fps)"


class TensorizeAdapter:
    """Convert raw_rgb image → float32 tensor."""
    def accepts(self, c: IOContract) -> bool:
        return c.media == "image" and c.format == "raw_rgb"

    def produces_for(self, inp: IOContract) -> IOContract:
        return IOContract(
            media="tensor", format="float32",
            shape=inp.shape, rate=inp.rate, extra=inp.extra
        )

    def cost(self) -> float:
        return 1.0

    def describe(self) -> str:
        return "TensorizeAdapter(raw_rgb→float32 tensor)"


# Registry of all available adapter types (order matters: decode before resize before tensorize)
ADAPTER_REGISTRY = [
    FormatDecodeAdapter(),
    TensorizeAdapter(),
    ResizeAdapter((640, 480, 3)),
]


def find_adapter_chain(
    producer_out: IOContract,
    consumer_in: IOContract,
    max_depth: int = 4,
) -> Tuple[Optional[List], float]:
    """
    BFS to find shortest/cheapest adapter chain converting producer_out to consumer_in.
    Returns (chain, total_cost) or (None, inf) if impossible.
    Shape-flexible: if consumer_in.shape differs from producer_out.shape, inserts ResizeAdapter.
    """
    from d2a.contracts import contracts_compatible

    # Already compatible?
    ok, _ = contracts_compatible(producer_out, consumer_in)
    if ok:
        return [], 0.0

    # BFS over adapter applications
    # state: (current_contract, chain_so_far, total_cost)
    queue = [(producer_out, [], 0.0)]
    visited_keys = {_contract_key(producer_out)}

    while queue:
        current, chain, cost = queue.pop(0)
        if len(chain) >= max_depth:
            continue

        # Build candidate adapters dynamically (ResizeAdapter with target shape)
        candidates = _build_candidate_adapters(current, consumer_in)
        for adapter in candidates:
            if not adapter.accepts(current):
                continue
            next_contract = adapter.produces_for(current)
            next_key = _contract_key(next_contract)
            if next_key in visited_keys:
                continue
            visited_keys.add(next_key)
            next_chain = chain + [adapter]
            next_cost = cost + adapter.cost()

            ok, _ = contracts_compatible(next_contract, consumer_in)
            if ok:
                return next_chain, next_cost

            queue.append((next_contract, next_chain, next_cost))

    return None, float("inf")


def _contract_key(c: IOContract) -> str:
    return f"{c.media}|{c.format}|{c.shape}|{c.rate}"


def _build_candidate_adapters(current: IOContract, target: IOContract):
    """Produce the right set of adapters given current state and target."""
    adapters = [
        FormatDecodeAdapter(),
        TensorizeAdapter(),
    ]
    # Add resize if shapes differ
    if target.shape and current.shape and current.shape != target.shape:
        adapters.append(ResizeAdapter(target.shape))
    # Add rate throttle if needed
    if target.rate and current.rate and current.rate > target.rate:
        adapters.append(RateThrottleAdapter(target.rate))
    return adapters
