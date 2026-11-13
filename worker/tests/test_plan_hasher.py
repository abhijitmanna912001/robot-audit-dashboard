"""Unit tests for plan_hasher. No network, no database, no robot.

Run from the worker directory:

    .venv\\Scripts\\python.exe -m unittest discover -s tests -t . -v
"""

from __future__ import annotations

import hashlib
import re
import unittest

from plan_hasher import HASH_PREFIX, PlanHashError, canonicalize_actions, hash_actions

# Shared Python/TypeScript reference vector, supplied by Abhijit. The TypeScript
# side must produce the same canonical bytes and the same hash for this input.
# These three values are a contract: if this test fails, fix the hasher or raise
# it with Abhijit. Do not edit the expected values to make it pass.
SHARED_VECTOR_ACTIONS: list = [
    {
        "actionId": "a1",
        "type": "set_joint",
        "target": "joint_a",
        "value": 1.0,
        "unit": "rad",
        "reason": "x",
    },
    {
        "actionId": "a2",
        "type": "set_joint",
        "target": "joint_b",
        "value": 0.30000000000000004,
        "unit": "rad",
        "reason": "y",
    },
]
SHARED_VECTOR_EXPECTED_CANONICAL = (
    '[{"actionId":"a1","reason":"x","target":"joint_a","type":"set_joint","unit":"rad","value":1},'
    '{"actionId":"a2","reason":"y","target":"joint_b","type":"set_joint","unit":"rad",'
    '"value":0.30000000000000004}]'
)
SHARED_VECTOR_EXPECTED_HASH = (
    "sha256:e97478a64c5d373d46d3abf1d1e544643578eb8107159fe5abe96a183464c684"
)


class HashFormatTests(unittest.TestCase):
    def test_output_is_prefixed_lowercase_sha256_hex(self) -> None:
        result = hash_actions([{"a": 1}])
        self.assertRegex(result, r"^sha256:[0-9a-f]{64}$")

    def test_hash_is_sha256_of_the_canonical_bytes(self) -> None:
        actions = [{"b": 2, "a": 1}]
        canonical = canonicalize_actions(actions)
        self.assertEqual(canonical, b'[{"a":1,"b":2}]')
        self.assertEqual(
            hash_actions(actions), HASH_PREFIX + hashlib.sha256(canonical).hexdigest()
        )

    def test_empty_array_is_valid(self) -> None:
        self.assertEqual(canonicalize_actions([]), b"[]")
        self.assertEqual(
            hash_actions([]), HASH_PREFIX + hashlib.sha256(b"[]").hexdigest()
        )

    def test_same_input_gives_same_hash(self) -> None:
        actions = [{"x": [1, 2, {"y": None}]}]
        self.assertEqual(hash_actions(actions), hash_actions(actions))


class CanonicalizationTests(unittest.TestCase):
    def test_object_key_order_does_not_matter(self) -> None:
        self.assertEqual(
            hash_actions([{"a": 1, "b": {"c": 2, "d": 3}}]),
            hash_actions([{"b": {"d": 3, "c": 2}, "a": 1}]),
        )

    def test_array_order_is_preserved(self) -> None:
        first, second = {"n": 1}, {"n": 2}
        self.assertEqual(canonicalize_actions([first, second]), b'[{"n":1},{"n":2}]')
        self.assertNotEqual(hash_actions([first, second]), hash_actions([second, first]))

    def test_nested_array_order_is_preserved(self) -> None:
        self.assertNotEqual(
            hash_actions([{"v": [1, 2]}]), hash_actions([{"v": [2, 1]}])
        )

    def test_integral_float_matches_integer(self) -> None:
        # RFC 8785 serializes numbers the ECMAScript way, so 50 and 50.0 agree.
        # This is what keeps Python and JavaScript hashes aligned.
        self.assertEqual(canonicalize_actions([{"v": 50.0}]), b'[{"v":50}]')
        self.assertEqual(hash_actions([{"v": 50.0}]), hash_actions([{"v": 50}]))

    def test_strings_are_encoded_as_utf8(self) -> None:
        self.assertEqual(
            canonicalize_actions([{"r": "déplacer €"}]),
            '[{"r":"déplacer €"}]'.encode("utf-8"),
        )

    def test_tuple_is_accepted_as_an_array(self) -> None:
        self.assertEqual(hash_actions(({"a": 1},)), hash_actions([{"a": 1}]))


class RejectionTests(unittest.TestCase):
    def test_non_finite_numbers_are_rejected(self) -> None:
        for bad in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=bad):
                with self.assertRaises(PlanHashError):
                    hash_actions([{"value": bad}])

    def test_nested_non_finite_number_is_rejected(self) -> None:
        with self.assertRaises(PlanHashError):
            hash_actions([{"a": {"b": [1, 2, float("nan")]}}])

    def test_non_array_input_is_rejected(self) -> None:
        for bad in ({"actions": []}, "[]", None, 42):
            with self.subTest(value=bad):
                with self.assertRaises(PlanHashError):
                    hash_actions(bad)  # type: ignore[arg-type]

    def test_integer_outside_safe_range_is_rejected(self) -> None:
        with self.assertRaises(PlanHashError):
            hash_actions([{"n": 2**53 + 1}])

    def test_non_json_values_are_rejected(self) -> None:
        for bad in (b"bytes", {1, 2}, object()):
            with self.subTest(value=type(bad).__name__):
                with self.assertRaises(PlanHashError):
                    hash_actions([{"v": bad}])

    def test_non_string_object_key_is_rejected(self) -> None:
        with self.assertRaises(PlanHashError):
            hash_actions([{1: "x"}])

    def test_invalid_unicode_is_rejected(self) -> None:
        with self.assertRaises(PlanHashError):
            hash_actions([{"reason": "\ud800"}])

    def test_extreme_nesting_is_a_plan_hash_error_not_a_recursion_error(self) -> None:
        deep: list = []
        current = deep
        for _ in range(5000):
            inner: list = []
            current.append(inner)
            current = inner
        with self.assertRaises(PlanHashError):
            hash_actions([deep])

    def test_plan_hash_error_is_a_value_error(self) -> None:
        self.assertTrue(issubclass(PlanHashError, ValueError))


class SharedReferenceVectorTests(unittest.TestCase):
    def test_canonical_form_matches_shared_reference_vector(self) -> None:
        canonical = canonicalize_actions(SHARED_VECTOR_ACTIONS)
        self.assertEqual(canonical.decode("utf-8"), SHARED_VECTOR_EXPECTED_CANONICAL)

    def test_hash_matches_shared_reference_vector(self) -> None:
        self.assertEqual(hash_actions(SHARED_VECTOR_ACTIONS), SHARED_VECTOR_EXPECTED_HASH)
        self.assertTrue(re.fullmatch(r"sha256:[0-9a-f]{64}", SHARED_VECTOR_EXPECTED_HASH))

    def test_expected_hash_is_the_sha256_of_the_expected_canonical_form(self) -> None:
        # Ties the two published values together, independently of the hasher.
        digest = hashlib.sha256(SHARED_VECTOR_EXPECTED_CANONICAL.encode("utf-8")).hexdigest()
        self.assertEqual(HASH_PREFIX + digest, SHARED_VECTOR_EXPECTED_HASH)

    def test_reference_vector_number_edge_cases(self) -> None:
        # 1.0 must serialize as 1, and 0.1 + 0.2 must keep all its digits.
        self.assertIn('"value":1}', SHARED_VECTOR_EXPECTED_CANONICAL)
        self.assertEqual(SHARED_VECTOR_ACTIONS[1]["value"], 0.1 + 0.2)
        self.assertIn('"value":0.30000000000000004}', SHARED_VECTOR_EXPECTED_CANONICAL)


if __name__ == "__main__":
    unittest.main()
