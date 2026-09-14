@tool
extends RefCounted

const Common = preload("common.gd")
const Objects = preload("objects.gd")
const Values = preload("values.gd")
const Diagnostics = preload("diagnostics.gd")

static func execute(tree: SceneTree, root: Node, args: Dictionary, generation: String, target: String) -> Dictionary:
    var source: Variant = args.get("source", "")
    if not source is String or source.is_empty() or source.to_utf8_buffer().size() > 65536 or not args.get("params", {}) is Dictionary:
        return Common.failure("INVALID_ARGUMENT", "Supply a GDScript source up to 64 KiB and an optional params object")
    var object := Objects.resolve(root, args, generation)
    if (args.has("ref") or args.get("path", ".") != ".") and not is_instance_valid(object):
        return Common.failure("STALE_TARGET", "Selected execution object is unavailable")
    var script := GDScript.new()
    script.resource_path = "res://.godot/godotk/exec_" + Crypto.new().generate_random_bytes(12).hex_encode() + ".gd"
    script.source_code = source
    var logger := Diagnostics.new()
    logger.source = script.resource_path
    logger.operation_source = "res://addons/godotk/execution.gd"
    OS.add_logger(logger)
    var invocation := {"attempted": false}
    # Keep cleanup in the caller: GDScript runtime errors abort the failing function,
    # rather than providing catch/finally around an arbitrary native call.
    var raw: Variant = await _invoke(script, tree, root, object, args, generation, target, logger, invocation)
    OS.remove_logger(logger)
    var diagnostics := logger.snapshot()
    var response: Dictionary = raw if raw is Dictionary and raw.has("ok") else Common.failure("SCRIPT_ERROR", "Native invocation did not return a completion envelope")
    if diagnostics.attributed_error and response.ok:
        response = Common.failure("SCRIPT_ERROR", "Native diagnostics report a script error; earlier changes may remain")
    var details := {"generation": generation, "target": target, "source": script.resource_path,
        "effects": "may_have_changed", "entrypoint_attempted": invocation.attempted,
        "diagnostics": diagnostics.entries, "diagnostics_dropped": diagnostics.dropped}
    if response.ok:
        response.result.merge(details)
    else:
        response.error.details = details
    return response

static func _invoke(script: GDScript, tree: SceneTree, root: Node, object: Object, args: Dictionary, generation: String, target: String, logger: Logger, invocation: Dictionary) -> Dictionary:
    var response: Dictionary
    var error := script.reload()
    if error != OK:
        response = Common.failure("SCRIPT_PARSE_ERROR", "GDScript could not compile")
    elif script.get_instance_base_type() != "RefCounted" or not script.is_tool():
        response = Common.failure("SCRIPT_CONTRACT", "Use @tool, extends RefCounted, and func run(ctx)")
    else:
        var valid := false
        var constructor_valid := true
        for method in script.get_script_method_list():
            if method.name == "run" and method.args.size() == 1 and int(method.args[0].type) in [TYPE_NIL, TYPE_DICTIONARY] and not (int(method.flags) & METHOD_FLAG_STATIC):
                valid = true
            if method.name == "_init" and method.args.size() > method.default_args.size():
                constructor_valid = false
        if not valid or not constructor_valid:
            response = Common.failure("SCRIPT_CONTRACT", "Use run(ctx) or run(ctx: Dictionary), and an initializer with no required arguments")
        else:
            # Construction and invocation may mutate state, even when a callee subsequently fails.
            invocation.attempted = true
            var instance: RefCounted = script.new()
            if instance == null:
                response = Common.failure("SCRIPT_ERROR", "Could not construct the script (use a zero-argument initializer)")
            elif logger.snapshot().attributed_error:
                response = Common.failure("SCRIPT_ERROR", "Script initialization reported an error")
            else:
                var context := {"tree": tree, "root": root, "object": object, "target": target,
                    "generation": generation, "params": args.get("params", {})}
                var value: Variant = await instance.call("run", context)
                var codec := Values.new(generation)
                response = Common.success({"value": codec.encode(value), "truncated": codec.truncated})
    return response
