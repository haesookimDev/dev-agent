"""The real VM fixture must not disclose private inputs even when it fails."""

import base64

import pytest
from test_guest_runner_postgres import assert_no_sensitive_output, redacted_output


@pytest.mark.parametrize("encoded", [False, True])
def test_leak_detection_uses_fixed_failure_without_private_values(encoded):
    private = b"synthetic-guest-fixture-private-canary"
    leaked = base64.b64encode(private) if encoded else private
    output = b"fixture output: " + leaked
    assert redacted_output(output, [private]) == "fixture output: [redacted]"
    with pytest.raises(AssertionError) as caught:
        assert_no_sensitive_output(output, [private])
    assert str(caught.value) == "private fixture data escaped the diagnostic boundary"
    assert private.decode() not in str(caught.value)
    assert leaked.decode() not in str(caught.value)


def test_safe_fixture_output_and_empty_sensitive_entries_are_accepted():
    assert_no_sensitive_output(b"fixture passed", [b"", b"private-canary"])
