@tool
extends RefCounted

const LIMIT := 1048576
var peer := StreamPeerTCP.new()
var incoming := PackedByteArray()
var outgoing := PackedByteArray()
var failed := false

func start(port: int) -> void:
    failed = peer.connect_to_host("127.0.0.1", port) != OK

func enqueue(message: Dictionary) -> void:
    var payload := JSON.stringify(message).to_utf8_buffer()
    var length := payload.size()
    if length > LIMIT or outgoing.size() + length + 4 > LIMIT * 2:
        failed = true
        return
    outgoing.append_array(PackedByteArray([(length >> 24) & 255, (length >> 16) & 255, (length >> 8) & 255, length & 255]))
    outgoing.append_array(payload)

func poll() -> Array:
    var messages: Array = []
    peer.poll()
    if failed or peer.get_status() == StreamPeerTCP.STATUS_ERROR:
        failed = true
        return messages
    if peer.get_status() != StreamPeerTCP.STATUS_CONNECTED:
        return messages
    if not outgoing.is_empty():
        var sent := peer.put_partial_data(outgoing.slice(0, mini(65536, outgoing.size())))
        if sent[0] != OK:
            failed = true
            return messages
        outgoing = outgoing.slice(sent[1])
    var available := peer.get_available_bytes()
    if available > 0:
        var data := peer.get_data(mini(65536, available))
        if data[0] != OK:
            failed = true
            return messages
        incoming.append_array(data[1])
    if incoming.size() > LIMIT * 2:
        failed = true
        return messages
    for _index in 16:
        if incoming.size() < 4:
            break
        var size: int = (incoming[0] << 24) | (incoming[1] << 16) | (incoming[2] << 8) | incoming[3]
        if size <= 0 or size > LIMIT:
            failed = true
            break
        if incoming.size() < size + 4:
            break
        var parser := JSON.new()
        if parser.parse(incoming.slice(4, size + 4).get_string_from_utf8()) != OK or not parser.data is Dictionary:
            failed = true
            break
        messages.append(parser.data)
        incoming = incoming.slice(size + 4)
    return messages
