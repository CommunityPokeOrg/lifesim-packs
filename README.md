# lifesim-packs

Community content packs for [lifesim](https://github.com/CommunityPokeOrg/lifesim) — data-only JSON
packs that add events, actions, jobs, shop items, ailments and world laws to the game, plus a registry
index (`index.json`) so clients can discover packs and resolve their dependencies.

## Repository layout

```
index.json                    registry index — one entry per pack, incl. its requires list
schema/
  pack.schema.json            JSON Schema for a pack file (mirrors the engine's zod schema)
  index.schema.json           JSON Schema for index.json
packs/
  <pack-id>/
    pack.json                 the pack itself; directory name MUST equal the pack's id
scripts/
  validate.py                 stdlib-only validator (index↔packs consistency, dependency graph)
.github/workflows/validate.yml  CI: runs validate.py on every push/PR
```

## Pack format

A pack is a single JSON file. Minimum shape:

```json
{
  "id": "my-pack",
  "name": "My Pack",
  "version": "1.0.0",
  "description": "What this pack adds.",
  "requires": [
    { "pack": "other-pack-id", "version": ">=1.0.0" }
  ],
  "events": [ ... ],
  "actions": [ ... ],
  "jobs": [ ... ],
  "laws": [ ... ],
  "items": [ ... ]
}
```

The full format — conditions, effects, outcomes, `goto` chains — is documented in the
[lifesim README](https://github.com/CommunityPokeOrg/lifesim#pack-format) and described structurally by
`schema/pack.schema.json`. The engine's zod schema is authoritative.

### `requires` — pack dependencies

`requires` declares hard dependencies on other packs:

- `{ "pack": "other-pack-id" }` — any version
- `{ "pack": "other-pack-id", "version": ">=1.2.0" }` — a semver range
  (`>=`, `<=`, `>`, `<`, `^`, `~`, or a bare `1.2.0` for an exact match)

The loader topologically sorts packs so dependencies load first. Missing packs, unsatisfied version
ranges and dependency **cycles** are reported and block loading. A pack may not require itself.

### `job` conditions — runtime prerequisites

Inside conditions, `{ "kind": "job" }` matches employment. This is how packs gate actions and events
behind careers:

| Shape | Matches |
|---|---|
| `{ "kind": "job" }` | The character currently holds any job |
| `{ "kind": "job", "id": "accountant" }` | Currently employed in that job id |
| `{ "kind": "job", "title": "Accountant" }` | Currently employed with that job title |
| `{ "kind": "job", "field": "white_collar" }` | Currently employed in any job with that career field |
| `{ "kind": "job", ..., "ever": true }` | The character has held that job at any point (job history) |

`field` is a free-form career track label declared on each job (e.g. `white_collar`, `service`,
`labor`, `medical`, `public`). Packs that add jobs should always tag a `field` so other packs can
build prerequisites on career tracks rather than specific job ids.

## Example packs

The dependency chain below is the worked example of the architecture:

```
white-collar-jobs   (base — adds white_collar jobs + office events)
      ▲
white-collar-fraud  requires white-collar-jobs >=1.0.0
                    its fraud actions only unlock while you hold a {kind:job, field:white_collar} job
      ▲
organized-crime-ring  requires white-collar-fraud >=1.0.0  (transitive dependency)
                    launders the fraud pack's `wcf.dirty_money` counter through front businesses
```

- [`packs/white-collar-jobs/pack.json`](packs/white-collar-jobs/pack.json) — five `white_collar` jobs
  (mailroom → senior analyst, with `job`-condition career ladders) plus office-life events.
- [`packs/white-collar-fraud/pack.json`](packs/white-collar-fraud/pack.json) — embezzle, insider-trade
  and cook-the-books actions gated on `{ "kind": "job", "field": "white_collar" }`; a `wcf_heat`
  counter feeds a forced audit → SEC investigation → indictment event chain.
- [`packs/organized-crime-ring/pack.json`](packs/organized-crime-ring/pack.json) — laundering actions
  that consume `wcf.dirty_money`, demonstrating one pack building on another pack's state.

## The registry index

`index.json` lists every pack with its id, name, version, path and a copy of its `requires` list,
so a client can resolve a dependency graph without downloading every pack file. Entries are sorted
by id. When you add or change a pack, update its index entry in the same commit — the validator
checks that they stay in sync.

## Contributing a pack

1. Create `packs/<your-pack-id>/pack.json`. The directory name must equal the pack's `id`.
2. Add an entry to `index.json` (keep it sorted by id, mirror `requires` exactly).
3. Run `python3 scripts/validate.py` — no dependencies, Python 3 stdlib only.
4. Open a PR; CI runs the same check.

Content rules:

- Data only — no code, no external requests, no URLs pointing at shady places.
- Namespace your ids and flags with your pack id (e.g. `mypack.some_event`, `mypack_counter`) so
  packs can't collide.
- Declare `field` on every job you add; depend on fields (not job ids) for career prerequisites
  unless you genuinely need one specific job.
- Keep `requires` accurate — it's what makes packs composable.
