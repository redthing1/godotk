@tool
extends Logger

var mutex := Mutex.new()
var entries: Array = []
var dropped := 0
var source := ""
var operation_source := ""
var attributed_error := false

func _log_error(function: String, file: String, line: int, code: String, rationale: String, _editor_notify: bool, error_type: int, script_backtraces: Array[ScriptBacktrace]) -> void:
    var frames: Array = []
    var attributed := (not source.is_empty() and file == source) or (not operation_source.is_empty() and file == operation_source)
    for trace in script_backtraces:
        for index in range(mini(trace.get_frame_count(), 16)):
            var path := trace.get_frame_file(index)
            attributed = attributed or (not source.is_empty() and path == source) or (not operation_source.is_empty() and path == operation_source)
            if frames.size() < 16:
                frames.append({"file": path.left(512), "line": trace.get_frame_line(index), "function": trace.get_frame_function(index).left(256)})
    mutex.lock()
    attributed_error = attributed_error or (attributed and error_type != ERROR_TYPE_WARNING)
    if entries.size() < 32:
        entries.append({"file": file.left(512), "line": line, "function": function.left(256),
            "code": code.left(2048), "message": rationale.left(2048), "error_type": error_type,
            "attribution": "script_stack" if attributed else "concurrent_or_unattributed", "frames": frames})
    else:
        dropped += 1
    mutex.unlock()

func snapshot() -> Dictionary:
    mutex.lock()
    var result := {"entries": entries.duplicate(true), "dropped": dropped, "attributed_error": attributed_error}
    mutex.unlock()
    return result
