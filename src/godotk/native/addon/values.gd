@tool
extends RefCounted

# Output-only encoding. Native code constructs input values; no second expression language.
var generation: String
var remaining := 2048
var truncated := false
var text_remaining := 65536

func _init(scope: String) -> void:
    generation = scope

func object_info(object: Object) -> Dictionary:
    var info := {"type": "Object", "class": object.get_class(),
        "ref": generation + ":" + str(object.get_instance_id())}
    if object is Resource:
        info["resource_path"] = object.resource_path
    if object is Node and object.is_inside_tree():
        info["path"] = str(object.get_path())
    return info

func encode(value: Variant, depth: int = 0) -> Variant:
    remaining -= 1
    if remaining < 0 or depth > 8:
        truncated = true
        return {"type": "Truncated"}
    match typeof(value):
        TYPE_NIL, TYPE_BOOL:
            return value
        TYPE_INT:
            if value > 9007199254740991 or value < -9007199254740991:
                return {"type": "int", "value": str(value)}
            return value
        TYPE_FLOAT:
            if not is_finite(value):
                return {"type": "float", "value": str(value)}
            return value
        TYPE_STRING, TYPE_STRING_NAME, TYPE_NODE_PATH:
            var text := str(value)
            var allowed := mini(16384, text_remaining)
            text_remaining -= mini(text.length(), allowed)
            if text.length() > allowed:
                truncated = true
                return {"type": type_string(typeof(value)), "value": text.left(allowed), "truncated": true}
            if value is String:
                return text
            return {"type": type_string(typeof(value)), "value": text}
        TYPE_VECTOR2, TYPE_VECTOR2I:
            return {"type": type_string(typeof(value)), "value": [encode(value.x), encode(value.y)]}
        TYPE_VECTOR3, TYPE_VECTOR3I:
            return {"type": type_string(typeof(value)), "value": [encode(value.x), encode(value.y), encode(value.z)]}
        TYPE_VECTOR4, TYPE_VECTOR4I:
            return {"type": type_string(typeof(value)), "value": [encode(value.x), encode(value.y), encode(value.z), encode(value.w)]}
        TYPE_COLOR:
            return {"type": "Color", "value": [encode(value.r), encode(value.g), encode(value.b), encode(value.a)]}
        TYPE_OBJECT:
            return object_info(value) if is_instance_valid(value) else null
        TYPE_DICTIONARY:
            # Entries preserve non-string keys and avoid collisions with typed envelopes.
            var entries: Array = []
            for key in value:
                if remaining <= 0:
                    truncated = true
                    break
                entries.append([encode(key, depth + 1), encode(value[key], depth + 1)])
            return {"type": "Dictionary", "entries": entries, "size": value.size()}
        TYPE_ARRAY, TYPE_PACKED_BYTE_ARRAY, TYPE_PACKED_INT32_ARRAY, TYPE_PACKED_INT64_ARRAY, TYPE_PACKED_FLOAT32_ARRAY, TYPE_PACKED_FLOAT64_ARRAY, TYPE_PACKED_STRING_ARRAY, TYPE_PACKED_VECTOR2_ARRAY, TYPE_PACKED_VECTOR3_ARRAY, TYPE_PACKED_COLOR_ARRAY, TYPE_PACKED_VECTOR4_ARRAY:
            var items: Array = []
            for item in value:
                if remaining <= 0:
                    truncated = true
                    break
                items.append(encode(item, depth + 1))
            return {"type": type_string(typeof(value)), "items": items, "size": value.size()}
        TYPE_RECT2, TYPE_RECT2I, TYPE_TRANSFORM2D, TYPE_PLANE, TYPE_QUATERNION, TYPE_AABB, TYPE_BASIS, TYPE_TRANSFORM3D, TYPE_PROJECTION:
            return {"type": type_string(typeof(value)), "text": var_to_str(value)}
        _:
            return {"type": type_string(typeof(value)), "unsupported": true}
