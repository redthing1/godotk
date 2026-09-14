@tool
extends RefCounted

func run(ctx: Dictionary) -> Variant:
    ctx.object.position = Vector2(12, 34)
    await ctx.tree.process_frame
    return ctx.object.position
