# Voice + notification

When: every tick — run the `{"gates": []}` prune on every round, and publish only the gates the operator must decide (§ Gate triage classes C, D, E)

    **The notification follows the same test, through the plugin's publisher — never a vault command.** Once per round, with every gate the round raised:

    ```bash
    echo '{"gates": [{"owner": "<session id or pane id>", "text": "<the gate line>", "session": "<the BLOCKED session id>"}]}' \
      | python3 $P/notify-gate.py --layer fleet
    ```

    Send `{"gates": []}` on a round that raised none — the empty call prunes **your own** cleared gates only, never another manager's. **`--layer fleet` is required** (the ledger is per layer; this sweep is a subset). Publish only gates the operator must decide — § Gate triage classes **C, D, E** in the Manager Session runbook; **A and B** are yours to clear and stay silent. Surface the script's message rather than swallowing it. **Do not pre-filter for cross-layer duplicates** — the script de-dups by escalating session id and prints the skip; publish and let it decide.
