from __future__ import annotations


def test_main_uses_the_current_web_desktop_interface():
    import launch
    import main

    assert main.main is launch.main
