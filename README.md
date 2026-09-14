# godotk

Native Godot control through a Python CLI and GDScript add-on.
Requires Python 3.11+ and Godot 4.6+. Experimental.

## Usage

Install from this repository with `uv tool install .`. From a Godot project:

```sh
godotk install
godotk open --headless
godotk play
godotk inspect --target game
godotk inspect --target game --path Player --read position
godotk input --action ui_accept --frames 3
godotk close --discard
```

Use `--project PATH` and `--godot PATH` before the command to select a project
and engine. Commands return JSON; errors exit nonzero. See `godotk --help`.

`inspect --section properties|methods|signals` discovers native members;
`--filter`, `--offset`, and `--limit` narrow results. Paths are scene-relative.
Use returned `--ref` values to inspect resources; refs do not keep objects alive.

## Scripts

`godotk exec --target game --file operation.gd` runs native GDScript:

```gdscript
@tool
extends RefCounted

func run(ctx: Dictionary) -> Variant:
    ctx.object.position = Vector2(12, 34)
    await ctx.tree.process_frame
    return ctx.object.position
```

Select an object with `--path` or `--ref`, or use `--target editor`.
Context contains `tree`, `root`, `object`, `target`, `generation`, and
`params` from `--params JSON`. Use `res://` paths for project dependencies.
Results carry native type information, bounded diagnostics, and truncation flags.

## Boundaries

- Close editors before install/uninstall. Setup rewrites project settings and retains
  recovery copies. **Uninstall before exporting**; export stripping is incomplete.
- Native code and property getters are trusted, not sandboxed. No implicit save,
  undo, rollback, or cancellation. `play` follows Godot's save-on-play settings;
  `close --discard` acknowledges possible unsaved changes.
- A timeout may follow applied changes. Supply `--request-id` and query `outcome ID`
  instead of blindly retrying. `status` and `logs` remain available during execution.
- Errors normally report without debugger pauses; `play --debug-breaks` enables
  error pauses. Explicit breakpoints can still pause; nothing automatically resumes them.
- `capture` reads native viewports, never the desktop. Omit `--headless` for rendering.
  Rendered capture and cross-platform behavior are not fully validated; pointer
  input and managed save/refresh are not implemented.

## Development

```sh
uv run --locked python -m unittest discover -s tests
```

Set `GODOTK_TEST_GODOT` to an isolated, self-contained engine copy to include
headless integration tests. Tests use temporary projects; engine preferences need
separate isolation.
