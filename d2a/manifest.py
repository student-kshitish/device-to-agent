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
    "streaming": <bool>,                         # does subscribe() apply (default False)
    # derived-provenance (v1.5, optional): present iff this capability is
    # SYNTHESISED by a recipe rather than read from hardware. "derived": true makes
    # the other three REQUIRED; "derived" absent/false FORBIDS them.
    "derived": <bool>,
    "recipe": <str>,                             # recipe name that produced it
    "fidelity": <str>,                           # honest statement of what it can/can't do
    "cannot_detect": [<str>, ...]                # explicit blind spots
  }

  fieldspec / paramspec = {
    "type": "number"|"string"|"boolean"|"object"|"array",   (required)
    "items": "number"|"string"|"boolean"|"object",  (required iff type=="array";
                                                     forbidden otherwise; NO nested arrays)
    "unit": <str>,          (optional)
    "description": <str>,   (optional)
    "hz": <number>,         (optional; the provider's native sample cadence for this
                             field — v1.5. Absent = unknown → clamp fallback.)
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

from d2a import boundary as _boundary
from d2a.resource_probes import (
    RESOURCE_SENSITIVITY, DIAGNOSTIC_SENSITIVITY, INTERVENTION_SENSITIVITY,
)
from d2a.guardian.device_kinds import KIND_SENSITIVITY, KIND_PRIMITIVES

# ── vocabulary ───────────────────────────────────────────────────────────────

MANIFEST_MAX_BYTES = 4096          # publish rejects a manifest larger than this

# v1.5 added four OPTIONAL derived-provenance keys (see _DERIVED_KEYS below) — a
# capability that is SYNTHESISED by a recipe rather than read from hardware says so
# on-wire, so a discovering agent learns it is a substitute and its honest limits
# with no demo code. This is the manifest half of closing derivation protocol gap 3.
_DERIVED_KEYS = {"derived", "recipe", "fidelity", "cannot_detect"}
# v1.6 (Phase 7) added ONE optional top-level honesty key, valid on ANY manifest:
# "cannot_observe" — an explicit list of what this capability STRUCTURALLY cannot
# see, so a discovering agent learns the blind spots of a REAL reading with no
# demo code. It is deliberately distinct from the derived-only "cannot_detect"
# (which states the limits of a SUBSTITUTE): a diagnostic reads genuine state, it
# is not derived, yet it must still declare what its read-only vantage point
# cannot reach (e.g. whether the hardware physically works).
_OBSERVE_KEYS = {"cannot_observe"}
# v1.7 (Phase 8) added ONE optional top-level honesty key for the intervention
# (mutating) capabilities: "cannot_fix" — an explicit list of what a fixer
# STRUCTURALLY cannot repair (dead hardware, BIOS/firmware, anything needing a
# privilege the runtime lacks, and the bootstrapping limit: D2A cannot fix its own
# broken runtime). Sibling of cannot_observe; valid on any manifest.
_FIX_KEYS = {"cannot_fix"}
# v1.11 (Phase 11) added ONE optional top-level key: "boundary" — a declared,
# device-enforced operational lane (the MCP "roots" concept, adapted): the set
# of targets/params this capability may EVER act on, checked BEFORE the consent
# gate. Vocabulary + enforcement live in d2a/boundary.py (a leaf, like
# conditions.py). Valid ONLY on intervention-tier manifests in v1 — a declared
# boundary nobody enforces would look like protection, so other tiers reject it
# until they grow an enforcement point (diagnostics/derivation are follow-ups).
_BOUNDARY_KEYS = {"boundary"}
_TOP_LEVEL_KEYS = ({"description", "reading", "actions", "consent_tier", "streaming"}
                   | _DERIVED_KEYS | _OBSERVE_KEYS | _FIX_KEYS | _BOUNDARY_KEYS)
_SCALAR_TYPES = {"number", "string", "boolean", "object"}
_ALL_TYPES = _SCALAR_TYPES | {"array"}
# Third tier (v1.7): "intervention" — MUTATING capabilities, above sensitive.
# Deny-by-default with a double gate (bind approval + per-plan approval).
_CONSENT_TIERS = {"open", "sensitive", "intervention"}
# v1.5 added optional per-field "hz": the provider's native sample cadence for a
# reading field (closing derivation protocol gap 1). Absent means "unknown" and the
# derive contract-checker falls back to the device MAX_SAMPLE_HZ clamp.
_FIELD_KEYS = {"type", "unit", "description", "items", "format", "hz"}
_PARAM_KEYS = _FIELD_KEYS | {"required"}
_ACTION_KEYS = {"description", "params", "long_running", "mutating"}


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


def consent_tier_for_diagnostic(family: str) -> str:
    """Intrinsic consent tier of a diagnostic family (unknown → 'sensitive').
    All diagnostics are sensitive — see DIAGNOSTIC_SENSITIVITY (the SSOT)."""
    return DIAGNOSTIC_SENSITIVITY.get(family, "sensitive")


def consent_tier_for_intervention(family: str) -> str:
    """Intrinsic consent tier of an intervention family (unknown → 'intervention').
    All interventions are the third tier — see INTERVENTION_SENSITIVITY (SSOT)."""
    return INTERVENTION_SENSITIVITY.get(family, "intervention")


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
    if "hz" in spec:
        hz = spec["hz"]
        if isinstance(hz, bool) or not isinstance(hz, (int, float)) or hz <= 0:
            raise ManifestError(f"{where}: 'hz' must be a positive number")
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
        if "mutating" in aspec and not isinstance(aspec["mutating"], bool):
            raise ManifestError(f"actions.{aname}: 'mutating' must be a boolean")
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

    # ── derived-provenance keys (v1.5) ────────────────────────────────────────
    # A derived capability MUST carry all three provenance strings; a real
    # (non-derived) capability MUST NOT carry any of them (so "derived" is an
    # honest, non-forgeable-by-omission signal, not decoration).
    if "derived" in manifest and not isinstance(manifest["derived"], bool):
        raise ManifestError("'derived' must be a boolean")
    is_derived = bool(manifest.get("derived", False))
    if is_derived:
        for k in ("recipe", "fidelity"):
            if not isinstance(manifest.get(k), str) or not manifest.get(k):
                raise ManifestError(
                    f"a derived manifest requires a non-empty string '{k}'")
        cd = manifest.get("cannot_detect")
        if not isinstance(cd, list) or not all(isinstance(x, str) for x in cd):
            raise ManifestError(
                "a derived manifest requires 'cannot_detect' as a list of strings")
    else:
        present = sorted(k for k in ("recipe", "fidelity", "cannot_detect") if k in manifest)
        if present:
            raise ManifestError(
                f"a non-derived manifest must not carry derived-provenance keys "
                f"{present} (set 'derived': true, or remove them)")

    # ── cannot_observe (v1.6) / cannot_fix (v1.7) — optional, any manifest ────
    for _k in ("cannot_observe", "cannot_fix"):
        if _k in manifest:
            v = manifest[_k]
            if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
                raise ManifestError(f"'{_k}' must be a list of strings")

    # ── boundary (v1.11) — optional, INTERVENTION tier only ───────────────────
    # Validated against THIS manifest's actions (a boundary on a param no action
    # takes is rejected at publish). Rejected on other tiers: a declared boundary
    # with no enforcement point would be decoration masquerading as protection.
    if "boundary" in manifest:
        if tier != "intervention":
            raise ManifestError(
                "'boundary' is only valid on intervention-tier manifests in v1 "
                "(a boundary nobody enforces would look like protection)")
        try:
            _boundary.validate_boundary(manifest["boundary"], manifest)
        except _boundary.BoundaryError as e:
            raise ManifestError(str(e)) from e

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


# ── diagnostic manifests (Phase 7) — the READ-ONLY self-inspection surface ───
# Each diagnostic declares WHAT it observes (reading) and, via cannot_observe,
# what its read-only vantage point structurally CANNOT reach. Diagnosis is the
# read-only half of the fix loop; intervention is a later phase. consent_tier is
# always "sensitive" (system introspection) per DIAGNOSTIC_SENSITIVITY.
#
# The boolean state field (present/loaded/active/…) is condition-subscribable
# (eq-on-bool), so an agent can ask to be notified when e.g. a device node's
# `present` becomes false. "observable" flags whether that boolean is a CONFIRMED
# reading or an unknown (source absent) — never overload the boolean itself.

_DIAGNOSTICS = {
    "device_node_health": {
        "description": "Read-only health of a device node: existence, permissions, "
                       "and which PIDs hold it open. Never opens the device.",
        "reading": {
            "present":     {"type": "boolean", "description": "device node exists"},
            "readable":    {"type": "boolean", "description": "readable by this process (R_OK)"},
            "path":        {"type": "string",  "description": "the device node path inspected"},
            "holder_pids": {"type": "array", "items": "number",
                            "description": "PIDs holding an open fd on the node (best-effort)"},
            "holder_count": {"type": "number",
                             "description": "count of holder_pids; conditionable (0 == released)"},
            "observable":  {"type": "boolean", "description": "primary signal was inspectable"},
            "reason":      {"type": "string",  "description": "why the reading degraded, if it did"},
        },
        "cannot_observe": [
            "whether the sensor hardware is physically functional",
            "BIOS-level enablement of the device",
            "file descriptors held by processes owned by other users without privilege",
        ],
    },
    "kernel_module_health": {
        "description": "Read-only health of a kernel module: whether it is loaded "
                       "(/proc/modules) plus dmesg tail lines mentioning it.",
        "reading": {
            "loaded":          {"type": "boolean", "description": "module present in /proc/modules"},
            "module":          {"type": "string",  "description": "module name inspected"},
            "dmesg_available": {"type": "boolean", "description": "dmesg log was readable"},
            "dmesg_lines":     {"type": "array", "items": "string",
                                "description": "tail of dmesg lines mentioning the module"},
            "observable":      {"type": "boolean", "description": "/proc/modules was readable"},
            "reason":          {"type": "string",  "description": "why the reading degraded, if it did"},
        },
        "cannot_observe": [
            "kernel log lines when dmesg requires privilege (kernel.dmesg_restrict)",
            "whether the module's underlying hardware actually works",
            "module parameters, version drift, or taint state",
        ],
    },
    "service_health": {
        "description": "Read-only active-state of a systemd unit via systemctl "
                       "is-active / show (system or --user scope).",
        "reading": {
            "active":       {"type": "boolean", "description": "ActiveState == active"},
            "active_state": {"type": "string",  "description": "systemd ActiveState"},
            "sub_state":    {"type": "string",  "description": "systemd SubState"},
            "service":      {"type": "string",  "description": "unit name inspected"},
            "scope":        {"type": "string",  "description": "system | user"},
            "observable":   {"type": "boolean", "description": "systemctl was available"},
            "reason":       {"type": "string",  "description": "why the reading degraded, if it did"},
        },
        "cannot_observe": [
            "whether the service is doing useful work (only its systemd state)",
            "the unit's logs or last exit code",
            "services managed by a non-systemd init system",
        ],
    },
    "usb_power_health": {
        "description": "Read-only USB power state from sysfs: autosuspend control, "
                       "runtime status, and autosuspend delay. Never writes power policy.",
        "reading": {
            "present":              {"type": "boolean", "description": "USB device power sysfs exists"},
            "autosuspend":          {"type": "boolean", "description": "power/control == auto"},
            "control":              {"type": "string",  "description": "power/control (auto|on)"},
            "runtime_status":       {"type": "string",  "description": "power/runtime_status"},
            "autosuspend_delay_ms": {"type": "number", "unit": "ms",
                                     "description": "power/autosuspend_delay_ms (-1 if unknown)"},
            "path":                 {"type": "string",  "description": "sysfs device path inspected"},
            "observable":           {"type": "boolean", "description": "power sysfs was readable"},
            "reason":               {"type": "string",  "description": "why the reading degraded, if it did"},
        },
        "cannot_observe": [
            "actual electrical power draw in watts",
            "whether the downstream USB device is functioning",
            "hub or upstream port power budget",
        ],
    },
}


def diagnostic_manifest(family: str, target: str) -> dict:
    """
    Composed, validated manifest for a diagnostic capability of `family`, pointed
    at `target` (a device path / module / service / usb id). The target is woven
    into the human description only — the reading schema is fixed per family.
    consent_tier is the family's SSOT sensitivity (always sensitive). Raises
    ManifestError for an unknown family (no silent fallback for a bad wire word).
    """
    base = _DIAGNOSTICS.get(family)
    if base is None:
        raise ManifestError(f"unknown diagnostic family {family!r}; "
                            f"known: {sorted(_DIAGNOSTICS)}")
    tier = consent_tier_for_diagnostic(family)
    m = {
        "description": f"{base['description']} Target: {target}. Linux-only; degrades "
                       f"gracefully (observable=false) where its source is absent.",
        "reading": base["reading"],
        "cannot_observe": list(base["cannot_observe"]),
        "consent_tier": tier,
        "streaming": True,   # condition-subscribable through the event layer
    }
    return validate_manifest(m, tier)


# ── intervention manifests (Phase 8) — the MUTATING surface ──────────────────
# An intervention CHANGES device state to fix it. Each family declares its
# mutating action(s) (marked mutating:true), the diagnostic family it is PAIRED
# with (evidence justifying the fix + the post-action verify read against the same
# family), and a cannot_fix list — the honest blind spots of a fixer (dead
# hardware, BIOS/firmware, privilege it lacks, and the bootstrapping limit).
# consent_tier is always "intervention" (the third tier). Reversibility is a
# per-PLAN property (it depends on the concrete action/target), so it lives in the
# InterventionPlan, not the static manifest.

# family → its paired diagnostic family (evidence + verify are read against this).
INTERVENTION_PAIRED_DIAGNOSTIC = {
    "service_intervene":       "service_health",
    "process_release":         "device_node_health",
    "kernel_module_intervene": "kernel_module_health",
}

_INTERVENTIONS = {
    "service_intervene": {
        "description": "Mutating fixer for a systemd (user-scope) unit: start / stop / "
                       "restart it. Reversible (stop<->start). Paired diagnostic: service_health.",
        "actions": {
            "start":   {"description": "start the unit (systemctl --user start)", "mutating": True},
            "stop":    {"description": "stop the unit (systemctl --user stop)",   "mutating": True},
            "restart": {"description": "restart the unit (systemctl --user restart)", "mutating": True},
        },
        "cannot_fix": [
            "a service failing due to its own bug (a restart only bounces it)",
            "system-scope units without privilege",
            "the unit's config errors or missing dependencies",
            "dead hardware the service depends on",
        ],
    },
    "process_release": {
        "description": "Mutating fixer that releases a device node by signalling the process "
                       "holding it (default SIGTERM). IRREVERSIBLE — a kill has no undo. "
                       "Paired diagnostic: device_node_health.",
        "actions": {
            "release": {"description": "signal the PID holding the node so the device can reopen",
                        "mutating": True,
                        "params": {"pid":    {"type": "number", "required": True},
                                   "signal": {"type": "string", "required": False,
                                              "description": "TERM (default) | KILL | HUP"}}},
        },
        "cannot_fix": [
            "restoring a killed process (a kill is not reversible)",
            "processes owned by other users without privilege",
            "why the process was holding the device",
            "dead hardware behind the node",
        ],
    },
    "kernel_module_intervene": {
        "description": "Mutating fixer for a kernel module: load / unload it (modprobe). "
                       "Reversible (load<->unload). REQUIRES privilege (CAP_SYS_MODULE) — "
                       "refused at preflight otherwise. Paired diagnostic: kernel_module_health.",
        "actions": {
            "load":   {"description": "load the module (modprobe)",       "mutating": True},
            "unload": {"description": "unload the module (modprobe -r)",   "mutating": True},
        },
        "cannot_fix": [
            "loading a module without CAP_SYS_MODULE / root (refused at preflight)",
            "a module absent from the kernel's module tree",
            "hardware the module drives",
            "firmware / BIOS-level enablement",
        ],
    },
}


def intervention_manifest(family: str, target: str,
                          boundary: dict | None = None) -> dict:
    """
    Composed, validated manifest for an intervention capability of `family`,
    pointed at `target` (a systemd unit / device node / module name). consent_tier
    is always "intervention" (the third tier). Declares the mutating action(s) +
    cannot_fix. Raises ManifestError for an unknown family.

    `boundary` (v1.11, optional): the declared operational lane — per-key
    constraints on "target" / action params from the d2a.boundary vocabulary.
    Validated here against THIS manifest's actions, published signed, and
    enforced by the device BEFORE the consent gate. Absent → unchanged (compat).
    """
    base = _INTERVENTIONS.get(family)
    if base is None:
        raise ManifestError(f"unknown intervention family {family!r}; "
                            f"known: {sorted(_INTERVENTIONS)}")
    tier   = consent_tier_for_intervention(family)
    paired = INTERVENTION_PAIRED_DIAGNOSTIC.get(family, "")
    m = {
        "description": f"{base['description']} Target: {target}. Linux-only; MUTATING — "
                       f"each concrete plan needs its own owner approval.",
        "reading": {
            "family":            {"type": "string"},
            "target":            {"type": "string"},
            "paired_diagnostic": {"type": "string",
                                  "description": f"diagnostic family for evidence + verify ({paired})"},
            "mutating":          {"type": "boolean", "description": "always true — this fixes state"},
        },
        "actions": base["actions"],
        "cannot_fix": list(base["cannot_fix"]),
        "consent_tier": tier,
        "streaming": False,
    }
    if boundary is not None:
        m["boundary"] = boundary
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

# Same guard for every diagnostic family (Phase 7): a vocabulary regression in a
# diagnostic manifest fails at import, not silently at attach/publish time.
for _fam in _DIAGNOSTICS:
    diagnostic_manifest(_fam, "_validation_probe")

# And every intervention family (Phase 8).
for _fam in _INTERVENTIONS:
    intervention_manifest(_fam, "_validation_probe")
