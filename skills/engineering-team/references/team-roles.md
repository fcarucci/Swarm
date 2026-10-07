# Team roles and decisions

Each role invocation has a bounded deliverable, a durable brief path, task IDs, and a return to PM. PM may resume a compatible same-role agent where the host supports it; a return alone does not prove its slot is free. Roles can run in waves. A separate reviewer/QA/product context must never be manufactured by renaming or reusing the author. The shared Swarm board may show author messages to everyone; independent reviewers inspect the requirement and change directly instead of relying on those messages.

Seats: **mandatory** (always present): PM (the invoking agent), EL, QA, at least one engineer, and an independent judge. **Optional** (user's choice via `team.toml` or `swarm team --job J --add/--remove/--show`; check the effective set before staffing): `product_manager`, `build_engineer`, `reviewer`, `verifier`. Removing a mandatory seat is rejected.

| Role | Seat | Decision and deliverable | Handoff |
|---|---|---|---|
| PM, the invoking agent | mandatory | Own job activation, board join/read, schedule, root spawns, dependency/status record, original-request comparison, user questions and reports | Give product manager the request and scope; give EL the checked product baseline; route returned requests and findings |
| Product manager (`product_manager`) | optional | Own product brief, competitor research disposition, PR criteria, and product acceptance by criterion | Mark every inference; require PM to route a blocking ambiguity; hand revisioned baseline to EL; examine final candidate directly |
| Build engineer (`build_engineer`) | optional | Own build/CI and gating: gate on the rebased result, scoped fix rounds with CI as the parallel full gate, build-slot use, head-bound integration checks | Report gate results with the head sha; Rebase and refresh review/CI if main moved and the SHA changed. Without this role EL owns these duties |
| EL (`engineering_lead`) | mandatory | Own architecture, ER-to-PR map, final complexity tier, engineer count, assignments, integration order, and technical acceptance | Return a staffing request with task IDs, owners, dependencies, and risk; ask PM to launch roles; integrate reviewed tasks and freeze candidate |
| Engineer (`engineer`) | mandatory | Implement assigned task, produce immutable diff/commit and focused evidence, acknowledge and fix findings | Return change and evidence for PM-assigned review; return repaired change to the same reviewer for recheck |
| Peer reviewer (`reviewer` or another `engineer`) | optional (or another engineer) | Inspect another agent's delivered code and tests against requirement IDs, architecture, diff, and checks | Record severity and evidence before accepting author explanations; confirm every fix or valid disposition |
| QA (`qa`) | mandatory | Write or extend relevant tests, run applicable categories, record defects/retests and quality result | Hand test code to an independent engineer for review; send failures to PM for owner assignment; sign off only on frozen candidate |
| Verifier (`verifier`) | optional | Independently check a specific `DONE` claim, read only | Reply `VERIFIED` or `FAILED` with evidence; this does not replace peer review or product acceptance |
| Judge (`judge`) | mandatory | Independently decide whether the Swarm goal is met | Record `met` or `not_met` with `candidate=<id> req=<revision>`; PM handles a `not_met` gap and checks verdict freshness |

**Absent optional roles.** No `product_manager`: EL writes the PR criteria compactly (same `PR-n` IDs, observable criteria, exclusions), PM compares them with the request, and product acceptance is done by EL and QA together and recorded as such per criterion. No `build_engineer`: EL owns build/CI gating, rebased-result checks, and scoped fix rounds. Never silently drop a duty because its role is absent.

Custom-role signoffs are workflow evidence checked by PM. Only Swarm's built-in judge/verifier restrictions and spawn controls are enforced by code. A custom `reviewer` is an ordinary worker, not a read-only verifier. PM assigns reviewers, never the author.

## Size the team

| Tier | Allocation | Review and QA |
|---|---|---|
| Tiny: one bounded, low-risk component change | Three distinct contexts: PM combines product and EL duties in one compact brief (the tiny tier needs no product manager); engineer authors; separate reviewer checks | Reviewer performs applicable QA checks. If that reviewer writes test code, PM/EL or another engineer reviews those tests. Author gives neither review nor product acceptance. |
| Moderate: user-visible behavior, integration, or several criteria | Distinct EL, engineer pool, reviewer, QA, plus `product_manager` and `build_engineer` when the composition includes them (otherwise EL carries those duties); PM schedules waves | Review every delivered change. QA covers affected flows and directly reports quality; product (product manager, or EL+QA when absent) and EL accept separately. |
| Complex: multiple streams, sensitive data/security, migrations, public interfaces, or difficult recovery | Distinct roles; EL adds engineers and review/QA coverage per independent stream | Review each stream and integrated candidate; plan recovery and performance checks proportional to risk. |

PM's intake size is provisional. EL confirms or upgrades it after product and architecture discovery, recording why the staffing and review depth fit. A four-slot session is a scheduling constraint, not a four-person team. PM root-spawns team roles by default. Member nested spawns are only for synchronously needed short helpers within nonrefundable Swarm caps; never use them to probe host capacity. No host slot is assumed free merely because a role returned.

For moderate and complex jobs, a Swarm goal and one root-spawned judge are recommended. If PM omits them, it records why and still checks all product, EL, QA, and review evidence itself.

## Authority and rejection

- **Baseline:** PM compares the first PR set with the original user request and records each consequential inference. PM returns an unsupported inference or unapproved exclusion to the product manager for a corrected revision, or routes a consequential ambiguity to the user before dependent EL work. A product manager can flag an ambiguity as blocking; PM routes it to the user when prior authorization cannot settle it. There is no routine request for approval of every baseline.
- **Scope:** Any PR exclusion or deferral, change to user-promised behavior, or finding that changes user scope needs the user's decision via PM before dependent work. The product manager cannot redefine scope through its own acceptance. Technical disputes go to EL; product-fit disputes go to product manager; a disputed scope boundary goes to the user.
- **Review:** Every delivered change, including tests, receives independent review. The author neither selects the reviewer nor dispositions its own finding. Reviewer rechecks a fix. EL plus reviewer may accept a reasoned medium/low finding; an unfixed high/critical finding needs the user's explicit decision via PM with EL recommendation and appears in the final report.
- **QA:** QA writes or extends missing relevant tests. Acceptance/integration apply to affected flows, end-to-end to affected journeys, and performance when required or credibly at risk. Product manager owns user-facing thresholds and EL technical thresholds before measurement. QA records omissions; EL concurs. A missing threshold is unresolved, never a pass. Another engineer reviews QA test code.
- **Product acceptance:** Product manager names the frozen candidate and requirement revision for every PR result. It directly exercises user-visible criteria, recording method and observation. If direct exercise is impossible, PM records why and the exact QA evidence used. Product, EL, and QA results remain separate.
- **Rejection:** PM maps product, EL, QA, reviewer, verifier `FAILED`, or judge `not_met` to a task owner. Repair returns through affected review and tests. Changed requirements or candidate content reopen the affected task review and all final signoffs as defined in [artifact contracts](artifacts.md). PM never forces completion to hide a blocker.

For any PM action, a subagent posts a concise board update **and** returns a request containing `action`, `task_id`, `artifact_path`, and `blocker` to the invoking PM. It does not wait indefinitely for the main session to notice a post; the main session has no member hook injection.

## Configuring the team

- **Default and file.** Without a file, `product_manager` is on and `build_engineer` is off. The user sets the default in `team.toml` next to the swarm config (`~/.config/swarm/team.toml`, or the path in `$SWARM_TEAM_CONFIG`); `team.example.toml` ships as a template. There is no team section in the swarm `config.toml`.
- **Per job.** `swarm activate --job J --team product_manager,build_engineer` sets the composition at activation; `swarm team --job J --show|--add ROLE|--remove ROLE` changes it later. PM runs `--show` before every staffing wave and staffs the effective set. Removing a mandatory role is an error.
- **Plugin.** These commands are provided by the team plugin that ships inside this skill, not by swarm core; the plugin also loads `team.toml` and handles `activate --team`. Core swarm works without it. `swarm plugins` lists loaded plugins and load errors; if `swarm team` is unknown or the plugin failed to load, run `swarm plugins`, report the error to the user, and assume the default composition only after recording that assumption. The plugin API is documented with the swarm plugin documentation, not here.
