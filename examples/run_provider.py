"""
run_provider.py — run on MACHINE A (terminal 1).

Starts a DeviceRuntime, publishes all capabilities via UDP broadcast, and
serves incoming bind requests over TCP until Ctrl+C.

The printed IP:PORT is what you pass to run_agent.py on the other machine
as the --peer argument (handles AP-isolation / broadcast-blocked routers).
"""

import sys
import os
import time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from runtimes.device_runtime import DeviceRuntime

runtime = DeviceRuntime(name="provider")
runtime.start_swarm()
time.sleep(0.1)

ip, port = runtime.swarm.address
print()
print("=" * 60)
print("Provider ready — serving on LAN")
print("=" * 60)
print(f"  node_id      : {runtime.node_id}")
print(f"  device_class : {runtime.device_class}")
print(f"  address      : {ip}:{port}  (TCP)")
print(f"  udp_discovery: port {runtime.swarm.discovery_port}  (broadcast)")
print(f"  capabilities : {list(runtime.capabilities)}")
print()
print(f"On MACHINE B run:")
print(f"  python3 examples/run_agent.py {ip}:{port}")
print()
print("Waiting for bind requests... (Ctrl+C to stop)")
print()

try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    pass
finally:
    runtime.stop_swarm()
    print("\nProvider stopped.")
