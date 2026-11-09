#!/usr/bin/env bats

@test "DNS boot recovery behavior (Python standard library)" {
    run python3 "$BATS_TEST_DIRNAME/test_dns_recovery.py"
    printf '%s\n' "$output"
    [ "$status" -eq 0 ]
}

@test "DNS recovery installer preflight and rollback (temporary filesystem)" {
    run python3 "$BATS_TEST_DIRNAME/test_dns_recovery_install.py"
    printf '%s\n' "$output"
    [ "$status" -eq 0 ]
}
