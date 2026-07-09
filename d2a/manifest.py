"""
d2a/manifest.py — capability manifests: a signed, machine-readable
self-description carried in every capability record (v1.2).

D2A's equivalent of an MCP tool schema / A2A agent card: an agent learns what a
capability IS (its reading schema, its actions + params, its consent tier,
whether it streams) from the discovery record alone — no demo code required.
This is the prerequisite for a mechanical d2a→MCP bridge.

DESIGN: a SMALL FIXED VOCABULARY, deliberately not full JSON Schema (no $ref,
no oneOf, no deep nesting). Rationale: a fixed vocabulary is writable by hand,
diffable, verifiable at publish time, and translatable to MCP schemas
mechanically. The whole grammar:

  manifest = {
    "description": <str>,                       # one line, human-readable (required)
    "reading":  { <field>: <fieldspec> },       # what a data frame contains (optional)
    "actions":  { <name>: {"description": <str>,
                           "params": { <param>: <paramspec> }} },   (optional)
    "consent_tier": "open" | "sensitive",       # MUST equal the policy SSOT (required)
    "streaming": <bool>                          # does subscribe() apply (default False)
  }

  fieldspec / paramspec = {
    "type": "number"|"string"|"boolean"|"object"|"array",   (required)
    "items": "number"|"string"|"boolean"|"object",  (required iff type=="array";
                                                     forbidden otherwise; NO nested arrays)
    "unit": <str>,          (optional)
    "description": <str>,   (optional)
    "format": "hex",        (optional; ONLY on type=="string" — declares hex-encoded bytes)
    "required": <bool>      (paramspec only)
  }

CONSENT SSOT: consent_tier is NOT free text. It must equal the resource's
intrinsic sensitivity — RESOURCE_SENSITIVITY for resource capabilities,
KIND_SENSITIVITY for peripheral kinds (unknown → "sensitive"). The manifest
describes the resource's NATURE; whether a given device grants it is a
bind-time policy decision, never encoded here. validate_manifest enforces the
match.

LEAF MODULE: stdlib + the two (non-transport) sensitivity data modules only.
Never imports swarm/kademlia/runtime, so publish-time validation stays cheap
and cycle-free.
"""

import json

from d2a.resource_probes import RESOURCE_SENSITIVITY
from d2a.guardian.device_kinds import KIND_SENSITIVITY, KIND_PRIMITIVES

# ── vocabulary ───────────────────────────────────────────────────────────────

MANIFEST_MAX_BYTES = 4096          # publish rejects a manifest larger than this

_TOP_LEVEL_KEYS = {"description", "reading", "actions", "consent_tier", "streaming"}
_SCALAR_TYPES = {"number", "string", "boolean", "object"}
_ALL_TYPES = _SCALAR_TYPES | {"array"}
_CONSENT_TIERS = {"open", "sensitive"}
_FIELD_KEYS = {"type", "unit", "description", "items", "format"}
_PARAM_KEYS = _FIELD_KEYS | {"required"}
_ACTION_KEYS = {"description", "params", "long_running"}


class ManifestError(ValueError):
    """Raised when a manifest violates the fixed vocabulary, the size cap, or the
    consent SSOT. The message names the exact offending path."""


# ── consent single-source-of-truth ──────────────────────────────────────────

def consent_tier_for_resource(name: str) -> str:
    """Intrinsic consent tier of a resource capability (unknown → 'sensitive')."""
    return RESOURCE_SENSITIVITY.get(name, "sensitive")


def consent_tier_for_kind(kind: str) -> str:
    """Intrinsic consent tier of a peripheral kind (unknown → 'sensitive')."""
    return KIND_SENSITIVITY.get(kind, "sensitive")


# ── validation ───────────────────────────────────────────────────────────────

def _validate_spec(spec: dict, where: str, *, allow_required: bool) -> None:
    if not isinstance(spec, dict):
        raise ManifestError(f"{where}: spec must be an object")
    allowed = _PARAM_KEYS if allow_required else _FIELD_KEYS
    unknown = set(spec) - allowed
    if unknown:
        raise ManifestError(f"{where}: unknown keys {sorted(unknown)}")

    t = spec.get("type")
    if t not in _ALL_TYPES:
        raise ManifestError(f"{where}: type must be one of {sorted(_ALL_TYPES)}, got {t!r}")

    if t == "array":
        items = spec.get("items")
        if items is None:
            raise ManifestError(f"{where}: array requires 'items'")
        if items not in _SCALAR_TYPES:      # 'array' not in _SCALAR_TYPES → no nested arrays
            raise ManifestError(
                f"{where}: array 'items' must be one of {sorted(_SCALAR_TYPES)} "
                f"(no nested arrays), got {items!r}")
    elif "items" in spec:
        raise ManifestError(f"{where}: 'items' is only valid when type=='array'")

    fmt = spec.get("format")
    if fmt is not None:
        if fmt != "hex":
            raise ManifestError(f"{where}: only format 'hex' is supported, got {fmt!r}")
        if t != "string":
            raise ManifestError(f"{where}: format 'hex' is only valid on type=='string'")

    for k in ("unit", "description"):
        if k in spec and not isinstance(spec[k], str):
            raise ManifestError(f"{where}: '{k}' must be a string")
    if allow_required and "required" in spec and not isinstance(spec["required"], bool):
        raise ManifestError(f"{where}: 'required' must be a boolean")


def validate_manifest(manifest: dict, expected_consent_tier: str,
                      max_bytes: int = MANIFEST_MAX_BYTES) -> dict:
    """
    Validate a manifest against the fixed vocabulary + the consent SSOT + the
    size cap. Returns the manifest (with 'streaming' defaulted to False) on
    success; raises ManifestError otherwise.

    expected_consent_tier is the caller's SSOT value for this capability
    (consent_tier_for_resource / consent_tier_for_kind). The manifest's
    consent_tier MUST equal it.
    """
    if not isinstance(manifest, dict):
        raise ManifestError("manifest must be an object")

    unknown = set(manifest) - _TOP_LEVEL_KEYS
    if unknown:
        raise ManifestError(f"unknown top-level keys {sorted(unknown)}")

    desc = manifest.get("description")
    if not isinstance(desc, str) or not desc:
        raise ManifestError("'description' is required and must be a non-empty string")

    reading = manifest.get("reading", {})
    if not isinstance(reading, dict):
        raise ManifestError("'reading' must be an object")
    for field, spec in reading.items():
        _validate_spec(spec, f"reading.{field}", allow_required=False)

    actions = manifest.get("actions", {})
    if not isinstance(actions, dict):
        raise ManifestError("'actions' must be an object")
    for aname, aspec in actions.items():
        if not isinstance(aspec, dict):
            raise ManifestError(f"actions.{aname}: must be an object")
        a_unknown = set(aspec) - _ACTION_KEYS
        if a_unknown:
            raise ManifestError(f"actions.{aname}: unknown keys {sorted(a_unknown)}")
        adesc = aspec.get("description")
        if not isinstance(adesc, str) or not adesc:
            raise ManifestError(f"actions.{aname}: 'description' is required")
        if "long_running" in aspec and not isinstance(aspec["long_running"], bool):
            raise ManifestError(f"actions.{aname}: 'long_running' must be a boolean")
        params = aspec.get("params", {})
        if not isinstance(params, dict):
            raise ManifestError(f"actions.{aname}.params: must be an object")
        for pname, pspec in params.items():
            _validate_spec(pspec, f"actions.{aname}.params.{pname}", allow_required=True)

    tier = manifest.get("consent_tier")
    if tier not in _CONSENT_TIERS:
        raise ManifestError(f"'consent_tier' must be one of {sorted(_CONSENT_TIERS)}, got {tier!r}")
    if tier != expected_consent_tier:
        raise ManifestError(
            f"consent_tier {tier!r} contradicts the policy SSOT "
            f"({expected_consent_tier!r}) for this capability")

    streaming = manifest.get("streaming", False)
    if not isinstance(streaming, bool):
        raise ManifestError("'streaming' must be a boolean")

    out = {**manifest, "streaming": streaming}

    size = len(json.dumps(out, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    if size > max_bytes:
        raise ManifestError(f"manifest is {size} bytes, exceeds cap of {max_bytes} bytes")

    return out


# ── built-in manifests for core capabilities ─────────────────────────────────
# Static schemas (they describe the POSSIBLE fields, independent of a given
# machine's live values). Authored once, validated at import so a vocabulary
# regression fails loudly at startup, not silently at publish.

_COMPUTE = {
    "description": "CPU, memory and disk load of the host, sampled fresh on each read.",
    "reading": {
        "cpu_count":        {"type": "number", "description": "logical CPU count"},
        "arch":             {"type": "string", "description": "CPU architecture"},
        "load1":            {"type": "number", "unit": "ratio", "description": "1-min load average"},
        "mem_total_mb":     {"type": "number", "unit": "MB"},
        "mem_available_mb": {"type": "number", "unit": "MB"},
        "mem_used_percent": {"type": "number", "unit": "%"},
        "disk_free_gb":     {"type": "number", "unit": "GB"},
        "disk_used_pct":    {"type": "number", "unit": "%"},
        # Eventable live-frame scalars (v1.3). Dotted names are the exact
        # DataProvider frame paths (source.field) so a subscribe_event condition
        # on them resolves against the sampled reading with no name bridging.
        "cpu.util_pct":        {"type": "number", "unit": "%",
                                "description": "mean per-core CPU utilization"},
        "cpu.load1":           {"type": "number", "unit": "ratio"},
        "memory.used_percent": {"type": "number", "unit": "%"},
    },
    "consent_tier": "open",
    "streaming": True,
}

_SENSING = {
    "description": "Thermal zones and hardware sensor inputs of the host.",
    "reading": {
        "thermal_zones":  {"type": "number", "description": "count of thermal zones"},
        "sample_temps_c": {"type": "array", "items": "number", "unit": "C",
                           "description": "sample of current zone temperatures"},
        "sensor_inputs":  {"type": "number", "description": "count of hwmon sensor inputs"},
        "hwmons":         {"type": "array", "items": "string",
                           "description": "hardware monitor chip names"},
        # Eventable live-frame scalar (v1.3) — dotted DataProvider frame path.
        "thermal.max_temp_c": {"type": "number", "unit": "C",
                               "description": "hottest thermal zone, sampled live"},
    },
    "consent_tier": "open",
    "streaming": True,
}

_CAMERA = {
    "description": "Camera device presence and access metadata (no frames captured).",
    "reading": {
        "count":  {"type": "number", "description": "number of camera nodes present"},
        "nodes":  {"type": "array", "items": "string", "description": "device node paths"},
        "access": {"type": "string", "description": "access class from device policy"},
    },
    "consent_tier": "sensitive",
    "streaming": False,
}

_RESOURCE_MANIFESTS = {
    "compute": _COMPUTE,
    "sensing": _SENSING,
    "camera":  _CAMERA,
}

# Per-primitive descriptions for the raw peripheral surface.
_RAW_PRIMITIVE_DESC = {
    "list_entries": "list directory entries at the relay path",
    "read_bytes":   "read raw bytes from the device/path",
    "write_bytes":  "write raw bytes to the device/path",
    "stat":         "stat the path (size, mtime, mode)",
    "delete":       "delete an entry at the path",
    "open_stream":  "open the character stream",
    "read_stream":  "read buffered bytes from the stream",
    "write_stream": "write bytes to the stream",
    "close_stream": "close the character stream",
    "read_events":  "read raw input events (consent-gated)",
    "read_value":   "read the scalar sensor value",
}


def _raw_manifest(kind: str) -> dict:
    """Manifest for a raw_<kind> relay capability (the RAW surface, per DumbRelay
    primitives). The smart/Guardian surface is a separate capability (Phase B)."""
    actions = {}
    for prim in KIND_PRIMITIVES.get(kind, []):
        spec = {"description": _RAW_PRIMITIVE_DESC.get(prim, prim)}
        if prim in ("read_bytes", "read_stream", "read_events"):
            spec["params"] = {"length": {"type": "number", "required": False}}
        elif prim in ("write_bytes", "write_stream"):
            spec["params"] = {"data": {"type": "string", "format": "hex", "required": True}}
        actions[prim] = spec
    return {
        "description": f"Raw {kind} peripheral relayed over D2A with no intelligence.",
        "reading": {
            "kind": {"type": "string"},
            "path": {"type": "string"},
            "data": {"type": "string", "format": "hex",
                     "description": "raw bytes returned by a read, hex-encoded"},
        },
        "actions": actions,
        "consent_tier": consent_tier_for_kind(kind),
        "streaming": False,
    }


# ── smart (Guardian VSO) manifests — the SMART surface, per device kind ──────
# Authored from VirtualSmartObject.handle_request's action routing + advertised
# live_state, NOT the raw relay primitives (that's _raw_manifest / raw_<kind>).

_SMART = {
    "block_fs": {
        "description": "Smart storage: an indexed, searchable, backup-capable filesystem surface.",
        "reading": {"entries_indexed": {"type": "number"},
                    "free_bytes": {"type": "number", "unit": "bytes"}},
        "actions": {
            "index":    {"description": "index the storage tree"},
            "search":   {"description": "search indexed entries",
                         "params": {"query": {"type": "string", "required": True}}},
            "backup":   {"description": "back up to another relay",
                         "params": {"target_relay": {"type": "object", "required": True}}},
            "organize": {"description": "organize entries into a tidy layout"},
        },
    },
    "char_stream": {
        "description": "Smart sensor stream: a parsed, structured, tailable character stream.",
        "reading": {"buffer_bytes": {"type": "number", "unit": "bytes"}},
        "actions": {
            "collect": {"description": "collect from the stream for a duration",
                        "params": {"duration": {"type": "number", "required": False}}},
            "parse":   {"description": "parse buffered data by pattern",
                        "params": {"pattern": {"type": "string", "required": False}}},
            "tail":    {"description": "return the tail of the buffer"},
        },
    },
    "input_event": {
        "description": "Smart control input: decoded input events (consent-gated).",
        "reading": {"access": {"type": "string"}, "system_input": {"type": "boolean"}},
        "actions": {"decode_events": {"description": "decode raw input events into control frames"}},
    },
    "sensor_file": {
        "description": "Smart sensor: monitors a raw sensor file and returns verdicts.",
        "reading": {"value": {"type": "number"}, "verdict": {"type": "string",
                    "description": "ok | warn | danger"}, "readable": {"type": "boolean"}},
        "actions": {
            "monitor": {"description": "sample the sensor N times",
                        "long_running": True,   # intervals*delay loop — runs async, task_id now
                        "params": {"intervals": {"type": "number", "required": False},
                                   "delay": {"type": "number", "required": False}}},
            "verdict": {"description": "classify the current reading",
                        "params": {"warn_threshold": {"type": "number", "required": False},
                                   "danger_threshold": {"type": "number", "required": False}}},
        },
    },
    "raw_generic": {
        "description": "Smart raw device: hexdump/capture over an unknown device.",
        "reading": {"data": {"type": "string", "format": "hex"}},
        "actions": {
            "hexdump": {"description": "hexdump N bytes",
                        "params": {"length": {"type": "number", "required": False}}},
            "capture": {"description": "capture N bytes",
                        "params": {"length": {"type": "number", "required": False}}},
        },
    },
}


def smart_manifest(kind: str) -> dict:
    """Composed manifest for a Guardian VirtualSmartObject's SMART surface, by kind.
    consent_tier is the kind's SSOT sensitivity. Validated before return."""
    base = _SMART.get(kind, {"description": f"Smart {kind} device.", "reading": {}, "actions": {}})
    m = {
        "description": base["description"],
        "reading": base.get("reading", {}),
        "actions": base.get("actions", {}),
        "consent_tier": consent_tier_for_kind(kind),
        "streaming": False,
    }
    return validate_manifest(m, consent_tier_for_kind(kind))


# ── emergent (Synthesis) manifests — the EMERGENT surface only ───────────────
# Composed from the synthesis kind alone (NEVER from member records). The member
# device kind fixes the consent tier via the same SSOT.

_EMERGENT_MEMBER_KIND = {
    "pooled_storage": "block_fs",
    "tiered_memory":  "block_fs",
    "merged_stream":  "char_stream",
    "sensor_array":   "sensor_file",
}

_EMERGENT = {
    "pooled_storage": {
        "description": "Emergent pooled storage fused from multiple block-fs members.",
        "actions": {
            "write": {"description": "write bytes under a key (routed to a member)",
                      "params": {"key": {"type": "string", "required": True},
                                 "data": {"type": "string", "format": "hex", "required": True}}},
            "read":  {"description": "read bytes for a key",
                      "params": {"key": {"type": "string", "required": True}}},
        },
    },
    "tiered_memory": {
        "description": "Emergent tiered memory: hot LRU fast tier over a slow-tier member.",
        "actions": {
            "put": {"description": "store a value (hot tier, spills to slow)",
                    "params": {"key": {"type": "string", "required": True},
                               "value": {"type": "string", "format": "hex", "required": True}}},
            "get": {"description": "fetch a value (fast then slow)",
                    "params": {"key": {"type": "string", "required": True}}},
        },
    },
    "merged_stream": {
        "description": "Emergent merged stream fused from multiple char-stream members.",
        "actions": {
            "read_merged": {"description": "round-robin read across members",
                            "params": {"max_per_member": {"type": "number", "required": False}}},
            "tail_all":    {"description": "tail all member streams",
                            "params": {"lines": {"type": "number", "required": False}}},
        },
    },
    "sensor_array": {
        "description": "Emergent sensor array fused from multiple sensor-file members.",
        "actions": {
            "read_all":    {"description": "read every member sensor"},
            "verdict_all": {"description": "classify every member reading",
                            "params": {"warn": {"type": "number", "required": True},
                                       "danger": {"type": "number", "required": True}}},
        },
    },
}


def emergent_manifest(kind: str, combined_contract: dict | None = None) -> dict:
    """
    Composed manifest for a synthesized EmergentDevice surface, from the synthesis
    kind + combined_contract ONLY — never from member records (no per-part leak).
    Scalar combined_contract entries become 'reading' fields.
    """
    base = _EMERGENT.get(kind, {"description": f"Emergent {kind} device.", "actions": {}})
    reading = {}
    for k, v in (combined_contract or {}).items():
        if isinstance(v, bool):
            reading[k] = {"type": "boolean"}
        elif isinstance(v, (int, float)):
            reading[k] = {"type": "number"}
        elif isinstance(v, str):
            reading[k] = {"type": "string"}
        # non-scalars (lists/dicts) are intentionally omitted — keep it a flat surface
    tier = consent_tier_for_kind(_EMERGENT_MEMBER_KIND.get(kind, ""))  # unknown → sensitive
    m = {
        "description": base["description"],
        "reading": reading,
        "actions": base.get("actions", {}),
        "consent_tier": tier,
        "streaming": False,
    }
    return validate_manifest(m, tier)


def builtin_manifest(cap) -> dict | None:
    """
    Return the validated built-in manifest for a Capability, or None if we do
    not ship one for it (records without a manifest remain valid — additive).

    `cap` is a d2a.schema.Capability; we read cap.name and cap.live_state only.
    """
    name = getattr(cap, "name", "")
    if name in _RESOURCE_MANIFESTS:
        m = _RESOURCE_MANIFESTS[name]
        return validate_manifest(m, consent_tier_for_resource(name))
    if name.startswith("raw_"):
        kind = getattr(cap, "live_state", {}).get("kind", name[len("raw_"):])
        m = _raw_manifest(kind)
        return validate_manifest(m, consent_tier_for_kind(kind))
    return None


# Fail loudly at import if a shipped manifest ever drifts out of the vocabulary.
for _n, _m in _RESOURCE_MANIFESTS.items():
    validate_manifest(_m, consent_tier_for_resource(_n))
