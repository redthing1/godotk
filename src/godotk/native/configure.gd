extends SceneTree

func _initialize() -> void:
    var args := OS.get_cmdline_user_args()
    if args.size() != 2 or args[0] not in ["install", "remove"]:
        quit(2)
        return
    var config := ConfigFile.new()
    if config.load(args[1]) != OK:
        quit(2)
        return
    var plugin_path := "res://addons/godotk/plugin.cfg"
    var runtime_path := "*res://addons/godotk/runtime.gd"
    var enabled: Variant = config.get_value("editor_plugins", "enabled", PackedStringArray())
    if not enabled is PackedStringArray:
        push_error("editor_plugins/enabled is not a PackedStringArray")
        quit(2)
        return
    if args[0] == "install":
        if config.has_section_key("autoload", "GodotK"):
            push_error("Autoload name GodotK is already in use")
            quit(2)
            return
        if plugin_path in enabled:
            push_error("Unowned godotk plugin entry already exists")
            quit(2)
            return
        enabled.append(plugin_path)
        config.set_value("autoload", "GodotK", runtime_path)
    else:
        if config.get_value("autoload", "GodotK", "") != runtime_path:
            push_error("GodotK autoload was changed; refusing to remove it")
            quit(2)
            return
        config.erase_section_key("autoload", "GodotK")
        var index: int = enabled.find(plugin_path)
        if index >= 0:
            enabled.remove_at(index)
    config.set_value("editor_plugins", "enabled", enabled)
    if config.save(args[1]) != OK:
        quit(2)
        return
    print("GODOTK_SETUP_OK")
    quit()
