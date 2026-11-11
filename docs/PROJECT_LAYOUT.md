# Project and file organization

## Keep the two kinds of organization separate

Resolve bins organize references and timelines inside the project database.
Filesystem folders hold source footage, graphics, Fusion assets, exports, and
backups. Moving a Media Pool entry does not move its source file. The current
implementation performs only bin moves and verifies that source paths stay the same.

## Bin layout

`setup_project.py` gives a project an `AI Bridge` root bin containing:

| Bin | Purpose |
| --- | --- |
| 01 Footage | Original source video references |
| 02 Audio | Music, dialogue, and effects when added |
| 03 Graphics | Stills, logos, and graphic assets when added |
| 04 VFX | Future VFX plates, references, and reusable assets |
| 05 Timelines | Editorial timelines; contains original Timeline 1 |
| 90 Tests | Disposable bridge fixtures and diagnostic timelines |

In the development test project, three existing video entries moved from Master to
01 Footage and Timeline 1 moved to 05 Timelines. Native media IDs, source paths, and original timeline content stayed
unchanged. Unknown media types are reported rather than assigned by a guess.

The original files stayed where they were on disk. They were not copied anywhere. Cross-project consolidation should be a
separate explicit operation with a reviewed file manifest and relink verification.

## Local state

Nothing is written inside the checkout. The state folder (`resolve-bridge paths`)
holds:

```text
operations.sqlite3             Durable mutation journal
operations.lock.sqlite3        Single-writer coordination
reports/                       Reports from the helper scripts
project_backups/               Exported .drp backups (or your backup_dir)
```

A DRP backup excludes source media. It is checked for archive integrity; it has
not been reimported or restored. Keep the original media available.

## Recommended next setup features

1. Build a reviewed project manifest before importing source media: stable source
   paths, media properties, intended bins, duplicates, missing files, and a proposed
   timeline frame rate/resolution.
2. Distinguish matching names from matching files. Surface duplicate names and
   shared source paths; never silently relink or deduplicate them.
3. Require an explicit choice for mixed-rate footage. This project has both 29.97
   and 30 fps sources. The bridge currently rejects mixed-rate insertions.
4. Add generated, plainly recognizable test fixtures under this repository, then
   import only those fixtures into 90 Tests. Keep their source media separate from
   the user's original assets.
5. Add reviewed consolidation and relinking only after verifying copies, preserving
   originals, and proving the resulting Media Pool paths and timeline state.

VFX can build on this structure: keep plates in 01 Footage, reference stills and
logos in 03 Graphics, and explicit effect assets in 04 VFX. Version compositions
and their inputs before exposing graph edits to an AI controller.
