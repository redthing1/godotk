@tool
extends RefCounted

static func success(result: Dictionary) -> Dictionary:
    return {"ok": true, "result": result}

static func failure(code: String, message: String) -> Dictionary:
    return {"ok": false, "error": {"code": code, "message": message, "details": {}}}

static func resolve(root: Node, args: Dictionary, generation: String) -> Node:
    if not is_instance_valid(root):
        return null
    var node: Node
    if args.has("ref"):
        var pieces := str(args.ref).split(":")
        if pieces.size() != 2 or pieces[0] != generation or not pieces[1].is_valid_int():
            return null
        var object: Object = instance_from_id(pieces[1].to_int())
        if object is Node:
            node = object
    else:
        node = root.get_node_or_null(NodePath(str(args.get("path", "."))))
    if not is_instance_valid(node) or (node != root and not root.is_ancestor_of(node)):
        return null
    return node

static func inspect_tree(root: Node, args: Dictionary, generation: String) -> Dictionary:
    if not root:
        return failure("NO_SCENE", "No scene is open/ready in this target")
    var node := resolve(root, args, generation)
    if not node:
        return failure("STALE_TARGET", "Node does not exist in this target; inspect again")
    var depth := int(args.get("depth", 2))
    var limit := int(args.get("limit", 128))
    if depth < 0 or depth > 5 or limit < 1 or limit > 256:
        return failure("INVALID_ARGUMENT", "Depth must be 0..5 and limit 1..256")
    var budget := {"count": 0, "limit": limit, "truncated": false}
    var result := _node(node, root, generation, depth, budget)
    return success({"generation": generation, "tree": result, "count": budget.count, "truncated": budget.truncated})

static func _node(node: Node, root: Node, generation: String, depth: int, budget: Dictionary) -> Dictionary:
    budget.count += 1
    var result := {"name": str(node.name), "class": node.get_class(), "path": str(root.get_path_to(node)),
        "ref": generation + ":" + str(node.get_instance_id()), "children": []}
    var script: Script = node.get_script()
    if script:
        result["script"] = script.resource_path
    if node is Node2D:
        result["position"] = {"type": "Vector2", "value": [node.position.x, node.position.y]}
    elif node is Node3D:
        result["position"] = {"type": "Vector3", "value": [node.position.x, node.position.y, node.position.z]}
    if node is CanvasItem:
        result["visible"] = node.is_visible_in_tree()
    if node is Control:
        result["rect"] = {"position": [node.position.x, node.position.y], "size": [node.size.x, node.size.y]}
        result["focused"] = node.has_focus()
        if node is Label or node is BaseButton or node is LineEdit:
            result["text"] = str(node.get("text")).left(256)
    if depth == 0:
        if node.get_child_count() > 0:
            budget.truncated = true
        return result
    for child in node.get_children():
        if budget.count >= budget.limit:
            budget.truncated = true
            break
        result.children.append(_node(child, root, generation, depth - 1, budget))
    return result

static func capture(tree: SceneTree, viewport: Viewport, generation: String, args: Dictionary) -> Dictionary:
    if DisplayServer.get_name() == "headless":
        return failure("RENDERING_UNAVAILABLE", "Headless/dummy rendering cannot produce an image; no desktop fallback")
    if not is_instance_valid(viewport):
        return failure("NO_VIEWPORT", "Selected viewport is unavailable")
    var max_width := int(args.get("max_width", 1280))
    if max_width < 64 or max_width > 4096:
        return failure("INVALID_ARGUMENT", "max_width must be 64..4096")
    var texture := viewport.get_texture()
    if not texture or texture.get_width() * texture.get_height() > 16777216:
        return failure("CAPTURE_LIMIT", "Viewport unavailable or exceeds the 16-megapixel readback limit")
    await RenderingServer.frame_post_draw
    if not is_instance_valid(viewport):
        return failure("STALE_TARGET", "Viewport disappeared before capture")
    texture = viewport.get_texture()
    if not texture or texture.get_width() * texture.get_height() > 16777216:
        return failure("CAPTURE_LIMIT", "Viewport grew beyond the readback limit")
    var image := viewport.get_texture().get_image()
    if not image or image.is_empty():
        return failure("CAPTURE_FAILED", "Godot returned no viewport image")
    var source_size := image.get_size()
    var conversion := "none"
    if viewport.use_hdr_2d:
        image.convert(Image.FORMAT_RGBA8)
        image.linear_to_srgb()
        conversion = "HDR2D to RGBA8/sRGB preview"
    if image.get_width() > max_width:
        image.resize(max_width, maxi(1, roundi(float(image.get_height()) * max_width / image.get_width())), Image.INTERPOLATE_LANCZOS)
    var directory := OS.get_environment("GODOTK_ARTIFACTS")
    if directory.is_empty() or DirAccess.make_dir_recursive_absolute(directory) != OK:
        return failure("ARTIFACT_FAILED", "No writable session artifact directory")
    # Host retention normally keeps 32 files. Also bound unacknowledged captures.
    if DirAccess.get_files_at(directory).size() >= 40:
        return failure("ARTIFACT_LIMIT", "Artifact directory is full; archive/remove unneeded images explicitly")
    var capture_id := Crypto.new().generate_random_bytes(16).hex_encode()
    var filename := directory.path_join(capture_id + ".png")
    if image.save_png(filename) != OK:
        return failure("ARTIFACT_FAILED", "Could not write the native image")
    return success({"capture": capture_id, "generation": generation, "path": filename, "mime": "image/png",
        "viewport_ref": generation + ":" + str(viewport.get_instance_id()),
        "width": image.get_width(), "height": image.get_height(), "source_size": [source_size.x, source_size.y],
        "frame": Engine.get_frames_drawn(), "process_frame": Engine.get_process_frames(),
        "color_conversion": conversion, "paused": tree.paused})
