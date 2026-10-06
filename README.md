# ChangeLedger

A GenLayer intelligent contract that tracks a set of **published fields on a
changing source** over time. Each observation is a node in a version chain, and
the change set is computed by the contract rather than reported by a model.

Bradbury testnet: `0x0438c77CFDb860a71ff72fD2D7bD3679dB37D10B`

---

## Why this is not another "fetch -> assess -> store" module

Two structural differences.

**1. The model's work and the decision are separate transactions.**

```
pull(watch_id, i)        one fetch + one extraction of source i. No decision.
aggregate(watch_id)      majority vote, diff, statuses. No model call at all.
```

`aggregate()` is pure deterministic code. Clearing every model mock does not stop
it — there is a test for exactly that. The model is only ever asked one narrow
question: *what does this source say these fields are?*

**2. State is a version chain, not a pile of records.**

Every version carries a `parent`, and `changed_fields` is the diff the contract
computed against that parent. "What changed and when" is a property of the
ledger, not something anyone has to assert.

| | one-shot assessor | this contract |
|---|---|---|
| transaction shape | fetch + assess + store in one call | `pull` (model) and `aggregate` (no model) are separate |
| the model's job | decide | extract a field map from one source |
| cross-source reasoning | the model | deterministic majority vote in the contract |
| change detection | none | `changed_fields` computed against the parent version |
| absent / split data | guessed | `MISSING` / `DISPUTED`, value left empty |
| a wrong record | stands | `dispute()` re-reads the source and corrects it |

## The deterministic core

### Majority vote per field

Across the sources pulled in a round, each field takes the **strict majority**
value. Anything else is refused rather than guessed:

| situation | result |
|---|---|
| strict majority agree on a non-empty value | `CONFIRMED`, that value |
| values split with no strict majority | `DISPUTED`, value `""` |
| the winning value is empty | `MISSING`, value `""` |

### Diff

`changed_fields` is `set(fields where parent value != current value)`. The model
never reports a change; the contract compares stored values.

### Correction

`dispute(watch_id, version, source_index, field, claimed_value)` names a field and
the value the caller believes one source actually carries. The contract re-reads
that source and applies the correction **only if the fresh reading supports the
claim and differs from what was stored**. The version is then marked `CORRECTED`
and re-aggregated from the corrected readings.

A claim the source does not support is rejected; so is disputing a non-latest
version, an unknown field, or a reading that already matches.

## Validation before anything is stored

- `fields`: non-empty, unique, bounded in length and count
- `sources`: non-empty, unique, `http(s)` only, bounded count
- `min_sources`: between 2 and the source count
- an extraction must name **exactly** the tracked field set — no missing, no extra
- every value must be a string within the length bound
- a source that cannot be retrieved produces **no reading** (`pull` raises), it
  never becomes an empty-but-successful one
- `pull` refuses a second read of the same source in the same round

## Testing

```
python -m pytest tests/direct -q
```

38 direct-mode tests covering:

- `start_watch` validation (fields, sources, `min_sources`, bounds)
- `pull`: reading storage, duplicate pull, out-of-range index, closed watch,
  empty body, HTTP 500, wrong field set, oversized value
- `aggregate`: needs `min_sources`, strict majority, split -> `DISPUTED`,
  absent -> `MISSING`, first-version parent, **diff reports only fields that
  actually changed**, version chain, and **no model call happens here**
- `dispute`: correction applied when a fresh reading supports it, rejected when
  it does not, already-matching claim, unknown field, bad index, oversized claim,
  non-latest version, missing reading
- ownership (`close_watch`) and every view

## Live on chain

Driven end to end on Bradbury against two real Wikipedia sources with the tracked
fields `title` and `lang`:

```
v1: parent=0  changed=['lang']  values={'title': '', 'lang': 'en'}
```

`lang` read `en` from both sources -> strict majority -> `CONFIRMED`. `title`
differed between the two sources -> `DISPUTED`, value left empty rather than
picked. That is the deterministic majority working on real fetched data.

## Contract surface

| Method | Kind | Purpose |
|---|---|---|
| `start_watch(subject, fields, sources, min_sources)` | write | register a monitored source set |
| `pull(watch_id, source_index)` | write | fetch + extract one source; no decision |
| `aggregate(watch_id)` | write | majority vote + diff + statuses; no model call |
| `dispute(watch_id, version, source_index, field, claimed_value)` | write | correct a reading a fresh read supports |
| `close_watch(watch_id)` | write | owner closes the watch |
| `get_watch(watch_id)` | view | watch incl. current values and statuses |
| `get_version(watch_id, n)` | view | a chain node: values, status, agreed, changed_fields |
| `get_reading(watch_id, round, source_index)` | view | the raw per-source extraction |
| `get_field(watch_id, field)` | view | current value + status |
| `get_history(watch_id)` | view | the whole chain in order |
| `get_watch_count()` / `get_version_count(watch_id)` | view | counters |
