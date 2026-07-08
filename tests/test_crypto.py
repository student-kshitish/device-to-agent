"""
Crypto-core tests (Step 2A) — d2a.crypto + d2a._ed25519_fallback ONLY.

No protocol/transport wiring is exercised here. Every test that touches
persistence points D2A_HOME at a per-test tmpdir, so the suite NEVER reads or
writes the real ~/.d2a.
"""

import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path

from d2a import crypto
from d2a import _ed25519_fallback as fb
from d2a.schema import KeyPair


class _TmpHome(unittest.TestCase):
    """Base: isolate D2A_HOME to a tmpdir for the duration of each test."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="d2a-crypto-test-")
        self._prev = os.environ.get("D2A_HOME")
        os.environ["D2A_HOME"] = self._tmp

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("D2A_HOME", None)
        else:
            os.environ["D2A_HOME"] = self._prev
        shutil.rmtree(self._tmp, ignore_errors=True)


# ── sign / verify round-trip + tamper detection ──────────────────────────────

class TestSignVerify(unittest.TestCase):

    def test_raw_round_trip(self):
        priv, pub = crypto.generate_keypair()
        msg = b"hello device-to-agent"
        sig = crypto.sign(msg, priv)
        self.assertEqual(len(sig), crypto.SIG_BYTES)
        self.assertTrue(crypto.verify(msg, sig, pub))

    def test_wrong_key_rejected(self):
        priv, pub = crypto.generate_keypair()
        _, other_pub = crypto.generate_keypair()
        sig = crypto.sign(b"payload", priv)
        self.assertFalse(crypto.verify(b"payload", sig, other_pub))

    def test_tampered_message_rejected(self):
        priv, pub = crypto.generate_keypair()
        sig = crypto.sign(b"amount=1", priv)
        self.assertFalse(crypto.verify(b"amount=2", sig, pub))

    def test_tampered_signature_rejected(self):
        priv, pub = crypto.generate_keypair()
        sig = bytearray(crypto.sign(b"payload", priv))
        sig[0] ^= 0x01
        self.assertFalse(crypto.verify(b"payload", bytes(sig), pub))

    def test_malformed_inputs_never_raise(self):
        _, pub = crypto.generate_keypair()
        self.assertFalse(crypto.verify(b"x", b"tooshort", pub))
        self.assertFalse(crypto.verify(b"x", b"\x00" * 64, "not-hex"))

    def test_short_private_key_raises(self):
        with self.assertRaises(ValueError):
            crypto.sign(b"x", "abcd")


# ── sign_dict / verify_dict (canonical dict signing) ─────────────────────────

class TestDictSigning(unittest.TestCase):

    def test_round_trip(self):
        priv, pub = crypto.generate_keypair()
        signed = crypto.sign_dict(
            {"type": "bind_request", "capability_name": "gpu", "ts": 123}, priv, pub
        )
        self.assertIn("sig", signed)
        self.assertEqual(signed["sig_key"], pub)   # sig_key is inside the payload
        self.assertTrue(crypto.verify_dict(signed))

    def test_field_tamper_detected(self):
        priv, pub = crypto.generate_keypair()
        signed = crypto.sign_dict({"type": "bind_request", "capability_name": "gpu"}, priv, pub)
        signed["capability_name"] = "camera"
        self.assertFalse(crypto.verify_dict(signed))

    def test_sig_key_substitution_detected(self):
        # Swap in a different (valid) key + its own signature and it must NOT
        # verify against a pinned key, and the substituted key must not match
        # the derivation of the original node.
        priv, pub = crypto.generate_keypair()
        signed = crypto.sign_dict({"type": "renew_binding"}, priv, pub)
        other_priv, other_pub = crypto.generate_keypair()
        # Attacker re-signs the same payload with their own key and swaps sig_key.
        forged = crypto.sign_dict({"type": "renew_binding"}, other_priv, other_pub)
        # Self-consistent (attacker signed it), but pin enforcement rejects it:
        self.assertTrue(crypto.verify_dict(forged))
        self.assertFalse(crypto.verify_dict(forged, expected_pubkey_hex=pub))

    def test_pin_enforced(self):
        priv, pub = crypto.generate_keypair()
        signed = crypto.sign_dict({"type": "bind_response"}, priv, pub)
        self.assertTrue(crypto.verify_dict(signed, expected_pubkey_hex=pub))
        _, wrong = crypto.generate_keypair()
        self.assertFalse(crypto.verify_dict(signed, expected_pubkey_hex=wrong))

    def test_missing_fields_rejected(self):
        self.assertFalse(crypto.verify_dict({"type": "x"}))
        self.assertFalse(crypto.verify_dict("not a dict"))

    def test_sig_field_not_part_of_signature(self):
        # verify_dict strips only "sig" (the output) before recomputing.
        priv, pub = crypto.generate_keypair()
        signed = crypto.sign_dict({"a": 1}, priv, pub)
        original = signed["sig"]
        signed["sig"] = "00" * crypto.SIG_BYTES   # garbage sig
        self.assertFalse(crypto.verify_dict(signed))
        signed["sig"] = original
        self.assertTrue(crypto.verify_dict(signed))


# ── canonical JSON stability ─────────────────────────────────────────────────

class TestCanonicalJSON(unittest.TestCase):

    def test_key_order_independent(self):
        a = crypto.canonical_json({"b": 2, "a": 1, "c": 3})
        b = crypto.canonical_json({"c": 3, "a": 1, "b": 2})
        self.assertEqual(a, b)

    def test_compact_separators(self):
        out = crypto.canonical_json({"a": 1, "b": [1, 2]})
        self.assertNotIn(b", ", out)
        self.assertNotIn(b": ", out)
        self.assertEqual(out, b'{"a":1,"b":[1,2]}')

    def test_unicode_preserved_not_escaped(self):
        out = crypto.canonical_json({"name": "café — 温度"})
        self.assertIn("café — 温度".encode("utf-8"), out)
        self.assertNotIn(b"\\u", out)

    def test_nested_key_order_independent(self):
        a = crypto.canonical_json({"outer": {"y": 1, "x": 2}})
        b = crypto.canonical_json({"outer": {"x": 2, "y": 1}})
        self.assertEqual(a, b)


# ── node_id derivation ───────────────────────────────────────────────────────

class TestDeriveNodeId(unittest.TestCase):

    def test_width_is_16_hex(self):
        _, pub = crypto.generate_keypair()
        nid = crypto.derive_node_id(pub)
        self.assertEqual(len(nid), 16)
        int(nid, 16)   # valid hex

    def test_stable_for_same_key(self):
        _, pub = crypto.generate_keypair()
        self.assertEqual(crypto.derive_node_id(pub), crypto.derive_node_id(pub))

    def test_distinct_keys_distinct_ids(self):
        _, pub1 = crypto.generate_keypair()
        _, pub2 = crypto.generate_keypair()
        self.assertNotEqual(crypto.derive_node_id(pub1), crypto.derive_node_id(pub2))

    def test_identity_matches(self):
        _, pub = crypto.generate_keypair()
        nid = crypto.derive_node_id(pub)
        self.assertTrue(crypto.identity_matches(nid, pub))
        self.assertFalse(crypto.identity_matches("deadbeefdeadbeef", pub))


# ── keypair persistence ──────────────────────────────────────────────────────

class TestKeypairPersistence(_TmpHome):

    def test_create_then_reload_stable(self):
        kp1 = crypto.load_or_create_keypair("node-a")
        kp2 = crypto.load_or_create_keypair("node-a")
        self.assertEqual(kp1.private_key, kp2.private_key)
        self.assertEqual(kp1.public_key, kp2.public_key)
        self.assertEqual(kp1.node_id, kp2.node_id)

    def test_node_id_derived_from_pubkey(self):
        kp = crypto.load_or_create_keypair("node-b")
        self.assertEqual(kp.node_id, crypto.derive_node_id(kp.public_key))

    def test_distinct_names_distinct_identities(self):
        a = crypto.load_or_create_keypair("alice")
        b = crypto.load_or_create_keypair("bob")
        self.assertNotEqual(a.node_id, b.node_id)

    def test_file_perms_0600(self):
        crypto.load_or_create_keypair("perm-node")
        path = Path(self._tmp) / "keys" / "perm-node.json"
        self.assertTrue(path.exists())
        mode = stat.S_IMODE(os.stat(path).st_mode)
        self.assertEqual(mode, 0o600, f"expected 0600, got {oct(mode)}")

    def test_tampered_node_id_in_file_ignored(self):
        kp = crypto.load_or_create_keypair("node-c")
        path = Path(self._tmp) / "keys" / "node-c.json"
        import json
        data = json.loads(path.read_text())
        data["node_id"] = "0000000000000000"   # attacker edits the stored id
        path.write_text(json.dumps(data))
        reloaded = crypto.load_or_create_keypair("node-c")
        self.assertEqual(reloaded.node_id, kp.node_id)   # re-derived, not trusted

    def test_keypair_repr_hides_seed(self):
        kp = crypto.load_or_create_keypair("secret-node")
        r = repr(kp)
        self.assertNotIn(kp.private_key, r)
        self.assertIn("<redacted>", r)


# ── TOFU pin store ───────────────────────────────────────────────────────────

class TestPinStore(_TmpHome):

    def _valid_identity(self):
        _, pub = crypto.generate_keypair()
        return crypto.derive_node_id(pub), pub

    def test_first_use_pins_and_accepts(self):
        store = crypto.PinStore()
        nid, pub = self._valid_identity()
        self.assertIsNone(store.verify(nid, pub))
        self.assertEqual(store.pinned_key(nid), pub)

    def test_same_key_reaccepted(self):
        store = crypto.PinStore()
        nid, pub = self._valid_identity()
        self.assertIsNone(store.verify(nid, pub))
        self.assertIsNone(store.verify(nid, pub))

    def test_pin_violation_distinct_from_derivation(self):
        store = crypto.PinStore()
        nid, pub = self._valid_identity()
        self.assertIsNone(store.verify(nid, pub))
        # Different key BUT crafted so node_id still derives from it → must be a
        # PIN violation, not a derivation violation. We do this by pinning nid to
        # pub, then presenting a second key whose derived id we force to match by
        # reusing nid (derivation check would pass only if the key derives to nid,
        # so instead we assert the two reasons are reported for the two cases).
        _, other_pub = crypto.generate_keypair()
        # other_pub does NOT derive to nid → derivation error dominates:
        self.assertEqual(store.verify(nid, other_pub), crypto.ERR_DERIVATION)

    def test_pin_violation_reason(self):
        # Construct a genuine pin violation: same node_id, a second key that
        # legitimately derives to it is infeasible, so we pin a node_id to a
        # DIFFERENT key directly and then present the real key.
        store = crypto.PinStore()
        nid, pub = self._valid_identity()
        _, mismatched = crypto.generate_keypair()
        # Manually seed a pin whose node_id derives from `pub` but is pinned to
        # `mismatched` — simulates a prior (attacker or corrupted) pin. Then the
        # real key presents: derivation passes (nid⇐pub) but pin differs.
        store._pins[nid] = mismatched
        self.assertEqual(store.verify(nid, pub), crypto.ERR_PIN)

    def test_derivation_violation_reason(self):
        store = crypto.PinStore()
        _, pub = crypto.generate_keypair()
        self.assertEqual(store.verify("deadbeefdeadbeef", pub), crypto.ERR_DERIVATION)

    def test_pins_persist_across_instances(self):
        nid, pub = self._valid_identity()
        crypto.PinStore().verify(nid, pub)
        fresh = crypto.PinStore()
        self.assertEqual(fresh.pinned_key(nid), pub)


# ── RFC 8032 §7.1 known-answer vectors (ground truth for the fallback) ────────

class TestRFC8032Vectors(unittest.TestCase):
    """
    Validate the pure-Python fallback directly against the official RFC 8032
    §7.1 Ed25519 test vectors. Run against d2a._ed25519_fallback EXPLICITLY,
    regardless of ACTIVE_BACKEND — this is the ground truth on a machine with
    no crypto backend installed. seed → expected pubkey, message → expected
    signature, byte-exact.
    """

    # (secret seed, public key, message, signature) — all hex; message "" = empty
    VECTORS = [
        # TEST 1 — empty message
        ("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
         "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a",
         "",
         "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155"
         "5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"),
        # TEST 2 — one byte
        ("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
         "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
         "72",
         "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da"
         "085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"),
        # TEST 3 — two bytes
        ("c5aa8df43f9f837bedb7442f31dcb7b166d38535076f094b85ce3a2e0b4458f7",
         "fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025",
         "af82",
         "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac"
         "18ff9b538d16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a"),
        # TEST SHA(abc) — message is SHA-512("abc"), 64 bytes
        ("833fe62409237b9d62ec77587520911e9a759cec1d19755b7da901b96dca3d42",
         "ec172b93ad5e563bf4932c70e1245034c35467ef2efd4d64ebf819683467e2bf",
         "ddaf35a193617abacc417349ae20413112e6fa4e89a97ea20a9eeee64b55d39a"
         "2192992a274fc1a836ba3c23a3feebbd454d4423643ce80e2a9ac94fa54ca49f",
         "dc2a4459e7369633a52b1bf277839a00201009a3efbf3ecb69bea2186c26b589"
         "09351fc9ac90b3ecfdfbc7c66431e0303dca179c138ac17ad9bef1177331a704"),
    ]

    def test_pubkey_derivation(self):
        for seed_hex, pub_hex, _msg, _sig in self.VECTORS:
            got = fb.secret_to_public(bytes.fromhex(seed_hex)).hex()
            self.assertEqual(got, pub_hex, f"pubkey mismatch for seed {seed_hex[:8]}…")

    def test_signature(self):
        for seed_hex, _pub, msg_hex, sig_hex in self.VECTORS:
            msg = bytes.fromhex(msg_hex)
            got = fb.sign(bytes.fromhex(seed_hex), msg).hex()
            self.assertEqual(got, sig_hex, f"signature mismatch for seed {seed_hex[:8]}…")

    def test_verify(self):
        for _seed, pub_hex, msg_hex, sig_hex in self.VECTORS:
            self.assertTrue(
                fb.verify(bytes.fromhex(pub_hex), bytes.fromhex(msg_hex), bytes.fromhex(sig_hex))
            )

    def test_verify_rejects_flipped_bit(self):
        _seed, pub_hex, msg_hex, sig_hex = self.VECTORS[2]
        sig = bytearray(bytes.fromhex(sig_hex))
        sig[0] ^= 0x01
        self.assertFalse(fb.verify(bytes.fromhex(pub_hex), bytes.fromhex(msg_hex), bytes(sig)))


# ── cross-backend parity (only meaningful when a real backend is installed) ───

class TestBackendParity(unittest.TestCase):

    def test_active_backend_matches_fallback_bytes(self):
        if crypto.using_fallback():
            self.skipTest("no real backend installed — nothing to compare against")

        seed = os.urandom(32)
        priv_hex = seed.hex()
        msg = b"parity across backends must be byte-identical"

        active_sig = crypto.sign(msg, priv_hex)          # real backend
        fallback_sig = fb.sign(seed, msg)                # pure python
        self.assertEqual(active_sig, fallback_sig,
                         "Ed25519 signatures must be byte-identical across backends")

    def test_pubkey_parity(self):
        if crypto.using_fallback():
            self.skipTest("no real backend installed")
        seed = os.urandom(32)
        self.assertEqual(crypto.public_from_private(seed.hex()), fb.secret_to_public(seed).hex())

    def test_cross_backend_verify(self):
        if crypto.using_fallback():
            self.skipTest("no real backend installed")
        seed = os.urandom(32)
        pub = fb.secret_to_public(seed)
        msg = b"signed by fallback, verified by active backend"
        sig = fb.sign(seed, msg)
        self.assertTrue(crypto.verify(msg, sig, pub.hex()))           # active verifies fallback
        self.assertTrue(fb.verify(pub, msg, crypto.sign(msg, seed.hex())))  # fallback verifies active


if __name__ == "__main__":
    unittest.main()
