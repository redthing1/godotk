extends Node2D

signal custom_event(amount: int)
@export_range(0.0, 1000.0) var speed: float = 300.0
@export var sample: Resource = preload("res://sample.tres")
var presses := 0
var releases := 0
var polled_frames := 0

func _get_property_list() -> Array[Dictionary]:
    return [{"name": "dynamic_value", "type": TYPE_INT, "usage": PROPERTY_USAGE_DEFAULT},
        {"name": "broken_getter", "type": TYPE_INT, "usage": PROPERTY_USAGE_EDITOR}]

func _get(property: StringName) -> Variant:
    if property == "dynamic_value":
        return 42
    if property == "broken_getter":
        var missing: Variant = null
        return missing.no_such_method()
    return null

func increment(amount: int = 1) -> int:
    presses += amount
    return presses

func fail_after_change() -> void:
    presses += 10
    var missing: Variant = null
    missing.no_such_method()

func emit_background_error() -> void:
    push_error("FIXTURE_BACKGROUND_ERROR")

func _ready() -> void:
    print("FIXTURE_READY autoload=", has_node("/root/FixtureAutoload"))

func _input(event: InputEvent) -> void:
    if event.is_action_pressed("ui_accept"):
        presses += 1
    if event.is_action_released("ui_accept"):
        releases += 1

func _process(_delta: float) -> void:
    if Input.is_action_pressed("ui_accept"):
        polled_frames += 1
    $State.text = "presses=%d releases=%d polled=%d held=%s" % [presses, releases, polled_frames, Input.is_action_pressed("ui_accept")]
