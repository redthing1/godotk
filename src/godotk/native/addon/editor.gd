@tool
extends EditorPlugin

const Wire = preload("wire.gd")
const Common = preload("common.gd")
const Objects = preload("objects.gd")
const Execution = preload("execution.gd")
var wire: RefCounted
var debugger: EditorDebuggerPlugin
var hello_sent := false
var welcomed := false
var busy := false
var generation := ""
var active_game: Dictionary = {}
var game_session := -1
var active_request := ""
var active_target := ""
var disconnected := false
var debug_breaks := false

class BridgeDebugger extends EditorDebuggerPlugin:
    signal received(message: String, data: Array, slot: int)
    signal ended(slot: int)
    signal pause_changed(slot: int, paused: bool)

    func _has_capture(prefix: String) -> bool:
        return prefix == "godotk"

    func _capture(message: String, data: Array, slot: int) -> bool:
        received.emit(message, data, slot)
        return true

    func _setup_session(slot: int) -> void:
        get_session(slot).stopped.connect(func(): ended.emit(slot))
        get_session(slot).breaked.connect(func(_can_debug: bool): pause_changed.emit(slot, true))
        get_session(slot).continued.connect(func(): pause_changed.emit(slot, false))

func _enter_tree() -> void:
    generation = OS.get_environment("GODOTK_SESSION")
    var token := OS.get_environment("GODOTK_TOKEN")
    if generation.is_empty() or token.is_empty():
        set_process(false)
        return
    wire = Wire.new()
    wire.start(int(OS.get_environment("GODOTK_PORT")))
    debugger = BridgeDebugger.new()
    debugger.received.connect(_game_message)
    debugger.ended.connect(_game_ended)
    debugger.pause_changed.connect(_game_pause_changed)
    add_debugger_plugin(debugger)

func _process(_delta: float) -> void:
    if not wire or disconnected:
        return
    var messages: Array = wire.poll()
    if wire.failed or (hello_sent and wire.peer.get_status() == StreamPeerTCP.STATUS_NONE):
        disconnected = true
        if game_session >= 0:
            debugger.get_session(game_session).send_message("godotk:deactivate", [])
        return
    if not hello_sent and wire.peer.get_status() == StreamPeerTCP.STATUS_CONNECTED and not EditorInterface.get_resource_filesystem().is_scanning():
        hello_sent = true
        wire.enqueue({"kind": "hello", "version": 1, "role": "editor", "token": OS.get_environment("GODOTK_TOKEN")})
    for message in messages:
        if message.get("kind") == "welcome":
            welcomed = true
        elif welcomed and message.get("kind") == "shutdown":
            EditorInterface.stop_playing_scene()
            get_tree().quit()
        elif welcomed and message.get("kind") == "request":
            _dispatch(message)

func _dispatch(request: Dictionary) -> void:
    if busy:
        _reply(str(request.get("id", "")), Common.failure("BUSY", "Native bridge already has an active operation"))
        return
    busy = true
    active_request = str(request.get("id", ""))
    active_target = str(request.get("target", "editor"))
    if request.get("target", "editor") == "game":
        if game_session < 0 or active_game.get("run", "") != request.get("run", ""):
            _finish(Common.failure("STALE_TARGET", "Game session changed"))
            return
        debugger.get_session(game_session).send_message("godotk:request", [request])
    else:
        _execute_editor.call_deferred(request)

func _execute_editor(request: Dictionary) -> void:
    var args: Dictionary = request.get("args", {})
    match request.get("op", ""):
        "inspect":
            if args.get("section", "tree") == "tree":
                _finish(Common.inspect_tree(EditorInterface.get_edited_scene_root(), args, generation))
            else:
                _finish(Objects.inspect_object(EditorInterface.get_edited_scene_root(), args, generation))
        "exec":
            _finish(await Execution.execute(get_tree(), EditorInterface.get_edited_scene_root(), args, generation, "editor"))
        "play":
            if EditorInterface.is_playing_scene():
                _finish(Common.failure("ALREADY_RUNNING", "Stop the current game before playing again"))
                return
            var scene := str(args.get("scene", ""))
            if not scene.is_empty() and (not scene.begins_with("res://") or not ResourceLoader.exists(scene, "PackedScene")):
                _finish(Common.failure("INVALID_SCENE", "Supply an existing res:// scene path"))
                return
            if scene.is_empty() and str(ProjectSettings.get_setting("application/run/main_scene", "")).is_empty():
                _finish(Common.failure("NO_MAIN_SCENE", "Project has no main scene; supply --scene"))
                return
            debug_breaks = bool(args.get("debug_breaks", false))
            if scene.is_empty():
                EditorInterface.play_main_scene()
            else:
                EditorInterface.play_custom_scene(scene)
            var deadline := Time.get_ticks_msec() + 15000
            while active_game.is_empty() or not active_game.get("ready", false):
                if Time.get_ticks_msec() >= deadline:
                    _finish(Common.failure("PLAY_NOT_READY", "Game launch did not reach scene readiness; inspect status/logs before retrying"))
                    return
                await get_tree().process_frame
            _finish(Common.success({"game": active_game, "save_policy": "Godot native play behavior", "debug_breaks": debug_breaks}))
        "stop":
            EditorInterface.stop_playing_scene()
            var deadline := Time.get_ticks_msec() + 5000
            while EditorInterface.is_playing_scene() or game_session >= 0:
                if Time.get_ticks_msec() >= deadline:
                    _finish(Common.failure("STOP_NOT_CONFIRMED", "Game stop not yet confirmed"))
                    return
                await get_tree().process_frame
            _finish(Common.success({"stopped": true}))
        "capture":
            var viewport: Viewport = EditorInterface.get_editor_viewport_2d()
            if args.get("view", "2d") == "3d":
                var index := int(args.get("index", 0))
                if index < 0 or index > 3:
                    _finish(Common.failure("INVALID_ARGUMENT", "3D viewport index must be 0..3"))
                    return
                viewport = EditorInterface.get_editor_viewport_3d(index)
            _finish(await Common.capture(get_tree(), viewport, generation, args))
        _:
            _finish(Common.failure("UNSUPPORTED_OPERATION", "Operation is not available in the editor target"))

func _game_message(message: String, data: Array, slot: int) -> void:
    if data.size() != 1 or not data[0] is Dictionary:
        return
    var value: Dictionary = data[0]
    if message == "godotk:ready":
        if value.get("session", "") != generation or (game_session >= 0 and game_session != slot):
            return
        game_session = slot
        active_game = value
        wire.enqueue({"kind": "game", "game": active_game})
    elif message == "godotk:result" and slot == game_session and value.get("run", "") == active_game.get("run", ""):
        if value.get("id", "") == active_request:
            _finish(value)

func _game_ended(slot: int) -> void:
    if slot != game_session:
        return
    game_session = -1
    active_game = {}
    wire.enqueue({"kind": "game", "game": null})
    # Lifecycle play/stop coroutines finish themselves; a game request died with it.
    if busy and active_target == "game":
        busy = false
        active_request = ""

func _game_pause_changed(slot: int, paused: bool) -> void:
    if slot == game_session and not active_game.is_empty():
        active_game["debugger_paused"] = paused
        wire.enqueue({"kind": "game", "game": active_game})

func _finish(result: Dictionary) -> void:
    _reply(active_request, result)
    busy = false
    active_request = ""
    active_target = ""

func _reply(request_id: String, result: Dictionary) -> void:
    var response := result.duplicate()
    response["id"] = request_id
    response["kind"] = "result"
    if JSON.stringify(response).to_utf8_buffer().size() > Wire.LIMIT:
        response = Common.failure("RESULT_TOO_LARGE", "Native result exceeds the wire budget; narrow the query")
        response["id"] = request_id
        response["kind"] = "result"
    wire.enqueue(response)

func _run_scene(_scene: String, args: PackedStringArray) -> PackedStringArray:
    if wire and not debug_breaks:
        args.insert(0, "--ignore-error-breaks")
    if wire and DisplayServer.get_name() == "headless":
        args.insert(0, "--headless")
    return args

func _exit_tree() -> void:
    if debugger:
        remove_debugger_plugin(debugger)
    if wire:
        wire.peer.disconnect_from_host()
