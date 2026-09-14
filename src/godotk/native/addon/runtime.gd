extends Node

const Common = preload("common.gd")
const Objects = preload("objects.gd")
const Execution = preload("execution.gd")
var run := ""
var active := false
var busy := false
var held: InputEvent
var capture_registered := false

func _ready() -> void:
    # Editor binaries only; never activate in debug/release export templates.
    if not OS.has_feature("editor") or OS.get_environment("GODOTK_SESSION").is_empty() or not EngineDebugger.is_active():
        set_process(false)
        return
    process_mode = Node.PROCESS_MODE_ALWAYS
    run = Crypto.new().generate_random_bytes(16).hex_encode()
    active = true
    EngineDebugger.register_message_capture("godotk", _message)
    capture_registered = true
    _announce.call_deferred()

func _announce() -> void:
    _send_ready(false)
    while active and (not get_tree().current_scene or not get_tree().current_scene.is_node_ready()):
        await get_tree().process_frame
    if active:
        await get_tree().process_frame
        _send_ready(true)

func _send_ready(ready: bool) -> void:
    EngineDebugger.send_message("godotk:ready", [{"run": run, "ready": ready, "pid": OS.get_process_id(),
        "session": OS.get_environment("GODOTK_SESSION"), "display": DisplayServer.get_name()}])

func _message(message: String, data: Array) -> bool:
    if message == "deactivate":
        active = false
        _release()
        return true
    if message != "request" or data.size() != 1 or not data[0] is Dictionary:
        return false
    var request: Dictionary = data[0]
    if not active or request.get("run", "") != run:
        _reply(str(request.get("id", "")), Common.failure("STALE_TARGET", "Run no longer active"))
    elif busy:
        _reply(str(request.get("id", "")), Common.failure("BUSY", "Runtime operation already active"))
    else:
        busy = true
        _execute.call_deferred(request)
    return true

func _execute(request: Dictionary) -> void:
    var args: Dictionary = request.get("args", {})
    var result: Dictionary
    match request.get("op", ""):
        "inspect":
            if args.get("section", "tree") == "tree":
                result = Common.inspect_tree(get_tree().current_scene, args, run)
            else:
                result = Objects.inspect_object(get_tree().current_scene, args, run)
        "exec":
            result = await Execution.execute(get_tree(), get_tree().current_scene, args, run, "game")
        "capture":
            result = await Common.capture(get_tree(), get_tree().root, run, args)
        "input":
            result = await _input_sequence(args)
        _:
            result = Common.failure("UNSUPPORTED_OPERATION", "Unsupported runtime operation")
    busy = false
    _reply(str(request.get("id", "")), result)

func _input_sequence(args: Dictionary) -> Dictionary:
    var frames := int(args.get("frames", 1))
    if frames < 1 or frames > 600 or (args.has("action") == args.has("key")):
        return Common.failure("INVALID_ARGUMENT", "Supply action OR key and a frame budget of 1..600")
    var event: InputEvent
    if args.has("action"):
        var action := str(args.action)
        if not InputMap.has_action(action):
            return Common.failure("UNKNOWN_ACTION", "Action does not exist in the running game's InputMap")
        var action_event := InputEventAction.new()
        action_event.action = action
        action_event.pressed = true
        action_event.strength = 1.0
        event = action_event
    else:
        var code := OS.find_keycode_from_string(str(args.key))
        if code == KEY_NONE:
            return Common.failure("UNKNOWN_KEY", "Godot did not recognize the key name")
        var key_event := InputEventKey.new()
        key_event.keycode = code
        key_event.pressed = true
        event = key_event
    held = event
    var start_process := Engine.get_process_frames()
    var start_physics := Engine.get_physics_frames()
    var start_time := Time.get_ticks_msec()
    Input.parse_input_event(event)
    Input.flush_buffered_events()
    for _index in frames:
        if not active:
            break
        await get_tree().process_frame
    _release()
    return Common.success({"delivered": true, "released": true, "active": active,
        "process_frames": Engine.get_process_frames() - start_process,
        "physics_ticks": Engine.get_physics_frames() - start_physics,
        "wall_ms": Time.get_ticks_msec() - start_time, "paused": get_tree().paused})

func _release() -> void:
    if not held:
        return
    var release: InputEvent = held.duplicate()
    release.set("pressed", false)
    if release is InputEventAction:
        release.strength = 0.0
    Input.parse_input_event(release)
    Input.flush_buffered_events()
    held = null

func _reply(request_id: String, result: Dictionary) -> void:
    if JSON.stringify(result).to_utf8_buffer().size() > 1000000:
        result = Common.failure("RESULT_TOO_LARGE", "Native result exceeds the wire budget; narrow the query (earlier effects may remain)")
    result["id"] = request_id
    result["run"] = run
    EngineDebugger.send_message("godotk:result", [result])

func _exit_tree() -> void:
    _release()
    if capture_registered:
        EngineDebugger.unregister_message_capture("godotk")
    active = false
