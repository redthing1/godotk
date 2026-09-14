@tool
extends RefCounted

const Common = preload("common.gd")
const Values = preload("values.gd")
const Diagnostics = preload("diagnostics.gd")

static func resolve(root: Node, args: Dictionary, generation: String) -> Object:
    if args.has("ref"):
        var pieces := str(args.ref).split(":")
        if pieces.size() != 2 or pieces[0] != generation or not pieces[1].is_valid_int():
            return null
        return instance_from_id(pieces[1].to_int())
    return Common.resolve(root, args, generation)

static func inspect_object(root: Node, args: Dictionary, generation: String) -> Dictionary:
    var logger := Diagnostics.new()
    logger.operation_source = "res://addons/godotk/objects.gd"
    OS.add_logger(logger)
    var raw: Variant = _inspect_object(root, args, generation)
    OS.remove_logger(logger)
    var response: Dictionary = raw if raw is Dictionary and raw.has("ok") else Common.failure("INSPECTION_ERROR", "Native inspection did not complete")
    var diagnostics := logger.snapshot()
    if diagnostics.attributed_error:
        response = Common.failure("INSPECTION_ERROR", "Native property discovery/read reported an error; getters may execute project code")
    var details: Dictionary = response.result if response.ok else response.error.details
    details["diagnostics"] = diagnostics.entries
    details["diagnostics_dropped"] = diagnostics.dropped
    return response

static func _inspect_object(root: Node, args: Dictionary, generation: String) -> Dictionary:
    var object := resolve(root, args, generation)
    if not is_instance_valid(object):
        return Common.failure("STALE_TARGET", "Object is unavailable; inspect again (refs do not retain objects)")
    var section := str(args.get("section", "properties"))
    var offset := int(args.get("offset", 0))
    var limit := int(args.get("limit", 64))
    var filter := str(args.get("filter", ""))
    var names: Variant = args.get("read", [])
    if section not in ["properties", "methods", "signals"] or offset < 0 or limit < 1 or limit > 128 or not names is Array or names.size() > 32:
        return Common.failure("INVALID_ARGUMENT", "Choose properties/methods/signals, offset >= 0, limit 1..128, at most 32 reads")
    var codec := Values.new(generation)
    var result := {"generation": generation, "object": codec.object_info(object), "section": section}
    var metadata: Array
    match section:
        "properties": metadata = object.get_property_list()
        "methods": metadata = object.get_method_list()
        "signals": metadata = object.get_signal_list()
    var matches: Array = []
    for item in metadata:
        if filter.is_empty() or str(item.name).containsn(filter):
            matches.append(item)
    var page: Array = []
    for index in range(mini(offset, matches.size()), mini(offset + limit, matches.size())):
        var item: Dictionary = matches[index].duplicate()
        if section == "properties":
            item["type_name"] = type_string(int(item.type))
            item["editor_visible"] = bool(int(item.usage) & PROPERTY_USAGE_EDITOR)
            item["stored"] = bool(int(item.usage) & PROPERTY_USAGE_STORAGE)
            item["read_only"] = bool(int(item.usage) & PROPERTY_USAGE_READ_ONLY)
        # Method defaults can contain native values; encode these explicitly.
        if item.has("default_args"):
            item["default_args"] = codec.encode(item.default_args)
        page.append(item)
    result.merge({"members": page, "total": matches.size(), "offset": offset,
        "next_offset": offset + page.size() if offset + page.size() < matches.size() else null})
    if not names.is_empty():
        var available := {}
        for item in object.get_property_list():
            available[str(item.name)] = int(item.usage)
        for name in names:
            if not name is String or not available.has(name) or (available[name] & (PROPERTY_USAGE_CATEGORY | PROPERTY_USAGE_GROUP | PROPERTY_USAGE_SUBGROUP)):
                return Common.failure("UNKNOWN_PROPERTY", "Read names must identify actual properties: " + str(name).left(128))
        var values := {}
        for name in names:
            if not is_instance_valid(object):
                return Common.failure("STALE_TARGET", "Object disappeared during property reads")
            values[name] = codec.encode(object.get(name))
        result["values"] = values
    result["truncated"] = codec.truncated
    return Common.success(result)
