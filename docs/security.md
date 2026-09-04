# Security invariants

These invariants are release blockers:

1. No repository write credential exists in a task VM before a recorded approver decision.
2. A task lease can address only its own work-item identifier.
3. Work-item transitions are both allow-listed and version checked.
4. GitHub and Slack webhook signatures are verified against their raw request bodies before parsing.
5. VM names and storage paths derive only from validated UUIDs; cleanup never accepts user paths or globs.
6. The libvirt socket, Docker socket, host home directories, and host SSH keys are never mounted into a task VM.
7. User console takeover is exclusive: acquiring it pauses all agent GUI input, and returning it produces an audit event.
8. Event and artifact metadata are recursively redacted before leaving the VM; raw credentials are never legitimate artifacts.
9. Preview and console routes expire and require an authenticated organization member.
10. An agent-generated security, architecture, migration, or dependency issue cannot self-apply its execution label.

Before enabling GitHub writes in production, add environment-level integration tests proving that a prompt-injected issue cannot obtain a write token, approve a run, address another lease, or modify a protected branch.
