# Private resource labels: local CLI contract

This is part of the local CLI candidate, not a deployed Skill change. Labels are
user-authored local metadata bound to a full resource reference. They are distinct
from shared node aliases, hardware facts and server task metadata.

## Commands

```sh
lockbot resource label add '<full ref>' 'training'
lockbot resource label view '<full ref>'
lockbot resource label list --label training --limit 5
lockbot resource label remove '<full ref>' 'training'
lockbot resource label clear '<full ref>'
lockbot resource label list --json
lockbot resource label list --json ref,labels
```

`add`, `remove` and `clear` are idempotent local updates. `view` returns an empty
label list for an unannotated reference. `list --label TEXT` matches an exact
private label after Unicode NFKC and case folding; it does not perform a resource
search. Display spelling is preserved, including the first spelling of an
already-added equivalent label. Labels must be trimmed, printable, nonempty and
at most 80 UTF-8 bytes before and after normalization, with at most eight labels
per reference. Common credential-shaped values are rejected.

All commands accept `--file /absolute/private/resource-labels.json`. The default
is `LOCKBOT_HOME/resource-labels.json`, or `~/.lockbot/resource-labels.json` when
the override is absent. Use a private directory outside Git. Reading a missing
store does not create a directory, store or lock. An update creates only the
private metadata it needs. The store is bounded to 1 MiB and 1,024 references;
an over-limit update fails without dropping older annotations.

Default output is a compact terminal view. `--json ref,labels` selects machine
output; `ref` is always retained. As with resource field discovery, bare
`--json` only lists fields and performs no action, including on update commands.
Use explicit fields to request JSON while adding or removing a label.

## Identity and privacy

Only canonical native `s-` references with a full 64-character scope digest and
canonical qualified external `e1` references are accepted. Abbreviated native
digests, names, IP suffixes, numeric bot IDs and unqualified provider `x-`
references are not annotation targets. Same-named resources in different scopes
remain separate. A reference can be annotated without checking whether it still
exists; use `resource view REF` for a fresh resource read.

These commands do not open connection catalogues or Keys, start providers, read
source configurations or send HTTP requests. Backend-selection flags supplied
by a launcher do not change this local behavior. Labels, their file path and the
private label filter never enter provider stdin, server APIs, shared aliases,
resource `--search` or mutation target selection. A label does not establish
hardware, ownership, availability or permission.

External reference namespaces remain bound to their explicit provider
registration/configuration. Reconfiguring a provider can leave annotations on
the old reference. The CLI does not transfer them to a new reference based on a
matching display name.

## Bounded local traversal

Lists default to twenty rows, with a maximum requested limit of 256 and an 8 KiB
whole-output cap. Rows retain their full references and are never clipped after
pagination advances. `has_more` and `next_cursor` provide continuation: repeat
the same local query with `--cursor TOKEN`.

The cursor binds the canonical metadata content, selected fields, normalized
label filter, limit and last returned reference's digest. It contains no label,
local path or raw resource reference. The unchanged store supplies the exact
anchor lookup. A content edit or changed query rejects the cursor; start a new
local traversal. This pagination never stores a resource snapshot or changes
the normal native/provider query cursors.

Updates use a stable cooperative OS lock, reread the latest metadata, and stage
a private file before replacement. Contention has a five-second checked bound.
Existing private-file ownership/permissions or Windows ACL requirements apply.
The Windows lock uses incompatible writer sharing while holding its handle;
the parent directory handle denies delete/rename sharing. These choices follow
the documented [Windows file-sharing rules](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilea).
Native Windows filesystem behavior requires its own runtime verification;
POSIX process-concurrency tests do not prove it.
