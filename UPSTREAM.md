# Upstream traceability

Eleven Energy Plus forked [`iPeel/HA-Eleven-Energy`](https://github.com/iPeel/HA-Eleven-Energy)
at `2aea070`. This table records each of the 28 upstream commits since then
and what this fork did with it, so nothing useful is lost and nothing is
taken over blindly.

**Last checked: 2026-11-09.** Upstream `main` is at `78de8d1` (2026-11-05);
the only other branch, `three-phase-and-controls` (`37893a9`), is already
merged into it. There is nothing newer to review.

Outcomes: **Ported** - the change is in this fork (adapted to the
`eleven_energy_plus` domain and its tests). **Already here** - the fork had
made the same change independently. **Not needed** - superseded upstream or
by a different design here, with the reason.

| Upstream commit | Date | Subject | Outcome in Eleven Energy Plus |
| --- | --- | --- | --- |
| `c71a32d` | 2026-10-20 | Add local brand assets | Already here: the fork has shipped its own `brand/` images since 1.5.0. |
| `4630ccf` | 2026-10-24 | Merge pull request #4 (brand assets) | Already here (as above). |
| `5951f67` | 2026-10-24 | Stop fighting Home Assistant over two things it now owns | Already here since 1.4.0: no `OptionsFlowHandler.__init__`, `async_get_options_flow` a `@staticmethod @callback`. |
| `24d1123` | 2026-10-24 | Report what the machine actually is, and make the actions targetable | Ported: per-phase entities only on three-phase models, per-battery readings. The targeting part was later superseded upstream by the Inverter field (`9de9712`), which is what this fork uses. |
| `e493ac9` | 2026-10-24 | Correct the entity ids already in the registry, and say so | Not needed: upstream reverted it in `14d4eca`; Home Assistant never stored the id wrongly. |
| `2bf6f22` | 2026-10-24 | Offer signing in, and keep the API key for those who need it | Ported: account sign-in with Eleven Energy's public client and PKCE, plus a fresh verifier per authorization and an API probe of every new credential. |
| `61fb373` | 2026-10-24 | Offer the work modes as device actions, and fail politely when the API is away | Ported: device actions delegating to the actions, validated when the automation loads; setup retries when the API is away. |
| `e2e62d8` | 2026-10-24 | Name the work modes as the business names them | Ported: Self Consumption, Grid Charge, Grid Export, Excess PV Export, Idle Battery, Target State Of Charge, Reset Work Mode. |
| `c1b2c5b` | 2026-10-24 | Choose an implementation before trying to authorise with it | Ported: the menu offers `pick_implementation`. |
| `4e4cf76` | 2026-10-24 | Register the OAuth implementation where the flow can actually find it | Ported: registered from the flow as well as `async_setup`. |
| `7f96620` | 2026-10-24 | Let an existing system change how it connects, and stop Options breaking it | Ported: Reconfigure switches credential kinds; the credential is not in Options. Credentials are also replaced wholesale and reloaded exactly once here. |
| `f736a39` | 2026-10-24 | Keep a measurement and its phases in the same part of the page | Ported: phase entities take their group's category. |
| `c7bdb0d` | 2026-10-24 | Show each solar tracker as its own set of readings | Ported: MPPT count from the model, items keyed by their own index (an unusable index is reported, never guessed). |
| `14d4eca` | 2026-10-24 | Stop migrating an entity id that was never stored wrongly | Consistent: `e493ac9` was never taken. This fork migrates only its own pre-2.0 positional entities. |
| `37893a9` | 2026-10-24 | Describe what the integration now does, and call it 2.0 | Ported into this README, rewritten for the fork. |
| `29296d8` | 2026-10-24 | Release 2.0: sign in, device actions, phases, batteries and strings | Ported as part of 2.0.0 (the fork keeps its own version numbers). |
| `94ea48f` | 2026-10-25 | Put a battery's own power where the rest of its readings are | Ported: Battery *n* Power is diagnostic. |
| `9dfe93d` | 2026-10-28 | Change license from Creative Commons to MIT | Ported: MIT. |
| `bb2fde6` | 2026-10-28 | Refactor service targets in services.yaml | Ported: no device filter on targets (hassfest rejects it). |
| `1ca3416` | 2026-10-28 | Update set_work_mode_self_consumption target | Ported (as above). |
| `23b245b` | 2026-10-28 | Move EntityCategory import in hybrid_inverter.py | Ported: `EntityCategory` from `homeassistant.const`; every Home Assistant import is checked for being public by the test suite. |
| `9de9712` | 2026-10-28 | Offer the inverter as a field, since a target may not filter devices | Ported: the Inverter field with a device selector; a multi-inverter site refuses an untargeted call instead of using the first inverter. |
| `9e40de3` | 2026-10-28 | Say why a key was refused instead of swallowing the reason | Ported: invalid credential, unreachable API, no inverter, different site and unexpected error are told apart; Home Assistant's session; unexpected errors logged with their traceback. |
| `049f1ac` | 2026-10-28 | Call it 2.2 | Not needed: version numbering is the fork's own. |
| `ca21318` | 2026-10-28 | Stop demanding a target an automation never needed | Ported: no `target` declared in `services.yaml`; `device_id` / `entity_id` still accepted. |
| `99ad741` | 2026-10-28 | Say whether Home Assistant ever asks for the device actions | Ported: debug log in `async_get_actions`. |
| `66d3545` | 2026-10-28 | Document the way in that a person can actually find | Ported: the README presents the actions first. |
| `78de8d1` | 2026-11-05 | Install through the Marketplace, where HACS now lives | Ported: Marketplace instructions for 2026.11+, HACS for 2026.3-2026.10. |

To re-check: `git fetch https://github.com/iPeel/HA-Eleven-Energy.git +refs/heads/*:refs/upstream/*`
then `git log --oneline 2aea070..refs/upstream/main`.
