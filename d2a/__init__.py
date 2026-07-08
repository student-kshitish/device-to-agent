from d2a.protocol import PROTOCOL_VERSION, ProtocolVersionError
from d2a.schema import Capability, BindRequest, BindToken, KeyPair, Binding
from d2a.contracts import IOContract, CapabilityContract, contracts_compatible
from d2a.adapters import (
    ResizeAdapter, FormatDecodeAdapter, ColorspaceAdapter,
    RateThrottleAdapter, TensorizeAdapter, find_adapter_chain,
    ADAPTER_REGISTRY,
)
from d2a.composer import Composer, CompositionPlan, Composition
from d2a.composition.atomic_binder import AtomicBinder
from d2a.composition.runtime_monitor import RuntimeMonitor
from d2a.composition.release_manager import ReleaseManager
from d2a import crypto
from d2a import manifest
from d2a.manifest import validate_manifest, ManifestError
from d2a.identity import generate_node_id, generate_keypair, derive_node_id
from d2a.verbs import (
    make_bind_request, make_bind_token, verify_token, verify_bind_token,
    make_binding, rebind, renew, unbind,
)
from d2a.broker import CapabilityBroker
from d2a.probes import probe_all, available_resources, PROBES
from d2a.swarm import SwarmTransport, LANSwarm
from d2a.swarm_dht import DHTSwarm
from d2a.stream_source import (
    SignalSource,
    CPUSource,
    MemorySource,
    GPUSource,
    ThermalSource,
    BatterySource,
    DiskIOSource,
    NetIOSource,
    CameraMetaSource,
    MicrophoneMetaSource,
    LocationMetaSource,
    DisplayMetaSource,
    StorageSource,
    NetworkMetaSource,
)
from d2a.preprocessor import Preprocessor
from d2a.data_provider import DataProvider
from d2a.resource_probes import probe_resources, RESOURCE_PROBES, RESOURCE_SENSITIVITY
from d2a.policy import ResourcePolicy
from d2a.sense_types import SenseRequest, SenseFrame, VALID_SHAPES, VALID_MODES, VERDICT_LEVELS, ADVICE
from d2a.sense_layer import SenseLayer
from d2a.guardian.relay import DumbRelay
from d2a.guardian.virtual_object import VirtualSmartObject
from agents.guardian_agent import GuardianAgent
from d2a.guardian.device_kinds import detect_kind, KIND_SENSITIVITY, KIND_PRIMITIVES
from d2a.composition.synthesis_types import (
    SynthesisSpec, EmergentDevice, SYNTHESIS_REGISTRY,
    MERGED_STREAM_POLICY, SENSOR_ARRAY_AGG,
)
from d2a.composition.synthesizer import Synthesizer
from d2a.composition.emergent_runtime import EmergentDeviceHandle

__all__ = [
    # protocol
    "PROTOCOL_VERSION",
    "ProtocolVersionError",
    # contracts + adapters
    "IOContract",
    "CapabilityContract",
    "contracts_compatible",
    "ResizeAdapter",
    "FormatDecodeAdapter",
    "ColorspaceAdapter",
    "RateThrottleAdapter",
    "TensorizeAdapter",
    "find_adapter_chain",
    "ADAPTER_REGISTRY",
    # composition
    "Composer",
    "CompositionPlan",
    "Composition",
    "AtomicBinder",
    "RuntimeMonitor",
    "ReleaseManager",
    # schema
    "Capability",
    "BindRequest",
    "BindToken",
    "KeyPair",
    "Binding",
    # crypto core (Ed25519 trust)
    "crypto",
    # capability manifests (v1.2)
    "manifest",
    "validate_manifest",
    "ManifestError",
    # identity
    "generate_node_id",
    "generate_keypair",
    "derive_node_id",
    # verbs
    "make_bind_request",
    "make_bind_token",
    "verify_token",
    "verify_bind_token",
    "make_binding",
    "rebind",
    "renew",
    "unbind",
    # broker
    "CapabilityBroker",
    # probes
    "probe_all",
    "available_resources",
    "PROBES",
    # resource probes
    "probe_resources",
    "RESOURCE_PROBES",
    "RESOURCE_SENSITIVITY",
    # policy
    "ResourcePolicy",
    # swarm
    "SwarmTransport",
    "LANSwarm",
    "DHTSwarm",
    # data delivery — signal sources
    "SignalSource",
    "CPUSource",
    "MemorySource",
    "GPUSource",
    "ThermalSource",
    "BatterySource",
    "DiskIOSource",
    "NetIOSource",
    "CameraMetaSource",
    "MicrophoneMetaSource",
    "LocationMetaSource",
    "DisplayMetaSource",
    "StorageSource",
    "NetworkMetaSource",
    # data delivery — pipeline
    "Preprocessor",
    "DataProvider",
    # sense layer
    "SenseRequest",
    "SenseFrame",
    "VALID_SHAPES",
    "VALID_MODES",
    "VERDICT_LEVELS",
    "ADVICE",
    "SenseLayer",
    # guardian / Case 2
    "DumbRelay",
    "GuardianAgent",
    "VirtualSmartObject",
    "detect_kind",
    "KIND_SENSITIVITY",
    "KIND_PRIMITIVES",
    # synthesis / Case 3
    "SynthesisSpec",
    "EmergentDevice",
    "SYNTHESIS_REGISTRY",
    "MERGED_STREAM_POLICY",
    "SENSOR_ARRAY_AGG",
    "Synthesizer",
    "EmergentDeviceHandle",
]
