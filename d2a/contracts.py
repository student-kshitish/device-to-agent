from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional, Tuple


@dataclass
class IOContract:
    media: str          # "image" | "audio" | "tensor" | "scalar"
    format: str         # "jpeg" | "raw_rgb" | "pcm16" | "float32" | "unknown"
    shape: Optional[tuple] = None   # None = any shape accepted
    rate: Optional[float] = None    # fps or hz; None = any rate accepted
    extra: dict = field(default_factory=dict)

    def __repr__(self):
        parts = [f"{self.media}/{self.format}"]
        if self.shape:
            parts.append(f"shape={self.shape}")
        if self.rate:
            parts.append(f"rate={self.rate}")
        return f"IOContract({', '.join(parts)})"


@dataclass
class CapabilityContract:
    name: str
    role: str                           # "producer" | "consumer" | "transform"
    produces: Optional[IOContract] = None
    accepts: Optional[IOContract] = None


def contracts_compatible(
    producer: IOContract,
    consumer: IOContract,
) -> Tuple[bool, str]:
    """
    Checks whether producer output satisfies consumer input requirements.
    Returns (True, "exact") or (False, detailed_reason).
    Never guesses — unknown format always fails.
    """
    # media must match exactly
    if producer.media != consumer.media:
        return False, f"media mismatch: producer={producer.media} consumer={consumer.media}"

    # unknown format on either side = cannot guarantee compatibility
    if producer.format == "unknown":
        return False, "producer format is unknown — cannot verify compatibility"
    if consumer.format == "unknown":
        return False, "consumer format is unknown — cannot verify compatibility"

    # format must match
    if producer.format != consumer.format:
        return False, f"format mismatch: producer={producer.format} consumer={consumer.format}"

    # shape check: consumer None = accepts any shape
    if consumer.shape is not None and producer.shape is not None:
        if producer.shape != consumer.shape:
            return False, (
                f"shape mismatch: producer={producer.shape} consumer={consumer.shape}"
            )

    # rate check: consumer None = accepts any rate; producer must meet or exceed consumer minimum
    if consumer.rate is not None and producer.rate is not None:
        if producer.rate < consumer.rate:
            return False, (
                f"rate inadequate: producer={producer.rate} < consumer_min={consumer.rate}"
            )

    return True, "exact"
