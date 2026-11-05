"""The Clear button: POST /api/clear."""

import time
from pathlib import Path

import pytest


def test_clear_needs_a_loaded_config(client_for) -> None:
    """With nothing loaded there is nothing to clear."""
    assert client_for().post("/api/clear").status_code == 409


def test_clear_when_idle_resets_to_loaded(make_config, client_for, wait_for) -> None:
    """After a finished run, clear drops the agent and trials but keeps the config."""
    client = client_for(make_config(iterations=2))
    client.post("/api/build")
    client.post("/api/run")
    finished = wait_for(client, {"finished", "failed"})
    assert finished["status"] == "finished", finished["error"]

    state = client.post("/api/clear").json()
    assert (state["status"], state["trials"]) == ("loaded", [])
    assert state["config"] == finished["config"]
    assert Path(finished["trials_path"]).exists()  # the saved CSV survives

    # Cleared means unbuilt: run is refused until the next build.
    assert client.post("/api/run").status_code == 409
    assert client.post("/api/build").json()["status"] == "built"


def test_clear_when_built_but_not_run(make_config, client_for) -> None:
    """A built agent that never ran is dropped straight away."""
    client = client_for(make_config(iterations=2))
    client.post("/api/build")
    assert client.post("/api/clear").json()["status"] == "loaded"


def test_clear_is_refused_until_a_stop_lands(make_config, client_for, wait_for) -> None:
    """Mid-run (or mid-stop) clear is refused; after Stop ends the run, it works."""
    client = client_for(make_config(iterations=50))
    client.post("/api/build")
    client.post("/api/run")
    _wait_for_a_trial(client)

    response = client.post("/api/clear")
    state = client.get("/api/state").json()
    assert response.status_code == 409, (state["status"], state["error"])
    assert "Stop the campaign" in response.json()["detail"]

    state = client.post("/api/stop").json()
    if state["status"] == "stopping":  # the stop hasn't landed yet
        assert client.post("/api/clear").status_code == 409

    stopped = wait_for(client, {"stopped", "failed"})
    assert stopped["status"] == "stopped", stopped["error"]
    rows = Path(stopped["trials_path"]).read_text().count("\n") - 1
    assert 1 <= rows < 50  # the interrupted campaign's trials were saved

    state = client.post("/api/clear").json()
    assert (state["status"], state["trials"]) == ("loaded", [])

    # And the session is usable again.
    assert client.post("/api/build").json()["status"] == "built"


def _wait_for_a_trial(client, timeout: float = 60) -> None:
    """
    Wait until the running campaign has completed at least one trial.

    Args:
        - client: The test client.
        - timeout: Seconds before failing.
    """
    deadline = time.monotonic() + timeout
    while not client.get("/api/state").json()["trials"]:
        if time.monotonic() > deadline:
            pytest.fail("no trial completed in time")
        time.sleep(0.2)
