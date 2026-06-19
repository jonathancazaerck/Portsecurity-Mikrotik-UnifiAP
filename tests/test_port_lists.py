from ap_switch_watchdog.port_lists import add_port, format_port_list, parse_port_list, remove_port


def test_parse_port_list_empty():
    assert parse_port_list("") == []
    assert parse_port_list(None) == []


def test_parse_port_list_multiple():
    assert parse_port_list("ether2,ether3") == ["ether2", "ether3"]


def test_format_port_list():
    assert format_port_list(["ether2", "ether3"]) == "ether2,ether3"
    assert format_port_list([]) == ""


def test_add_port_appends():
    assert add_port("ether2", "ether3") == "ether2,ether3"


def test_add_port_idempotent():
    assert add_port("ether2,ether3", "ether3") == "ether2,ether3"


def test_add_port_to_empty():
    assert add_port("", "ether2") == "ether2"
    assert add_port(None, "ether2") == "ether2"


def test_remove_port():
    assert remove_port("ether2,ether3,ether4", "ether3") == "ether2,ether4"


def test_remove_port_not_present():
    assert remove_port("ether2,ether3", "ether9") == "ether2,ether3"


def test_remove_port_from_empty():
    assert remove_port("", "ether2") == ""
