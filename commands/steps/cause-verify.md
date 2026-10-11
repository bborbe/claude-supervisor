**Delegate that verification.** One sub-agent per candidate cause (`Explore` or `general-purpose`, **Sonnet**), the cause as a yes/no question plus the systems to check; take back only the verdict:

> "Is `github-update-go-agent` actually broken right now? Check merged PRs on `bborbe/github-update-go-agent`, the latest released tag, and live Job states in `kubectl<wrapper> -n prod`. Answer: broken / fixed / can't tell, with the evidence line for each."

Independent causes go in one message so they run concurrently. **Never delegate** course-correction drafts. **Keep every `SendMessage` in this session** — a sub-agent has no address, so replies land here regardless.
