"""End-to-end checks for server.py on the local (simulated) path."""

import json
from pathlib import Path

import pytest
from xpd_autonomous_gui.server import acquire_lock


def test_local_campaign_runs_to_completion(make_config, client_for, wait_for) -> None:
    """Load, build, and run a 2-iteration local campaign through the API."""
    client = client_for(make_config(iterations=2))

    state = client.get("/api/state").json()
    assert (state["status"], state["mode"]) == ("loaded", "local")
    assert state["editable"] == "all"

    assert client.post("/api/run").status_code == 409  # not built yet
    assert client.post("/api/build").json()["status"] == "built"
    assert client.post("/api/run").json()["status"] == "running"

    state = wait_for(client, {"finished", "failed"})
    assert state["status"] == "finished", state["error"]
    assert len(state["trials"]) == 2
    # The fake measurement echoes the reference, so it correlates ~perfectly.
    assert state["trials"][0]["corr_Wanted"] > 0.99
    assert Path(state["trials_path"]).read_text().count("\n") == 3  # header + 2

    # Editing after the run rebuilds the agent but keeps the trials visible.
    state["config"]["run"]["iterations"] = 5
    state = client.put("/api/config", json=state["config"]).json()
    assert state["status"] == "loaded"
    assert len(state["trials"]) == 2


def test_running_edits_are_limited_and_success_criteria_stop_the_run(
    make_config, client_for, wait_for
) -> None:
    """Mid-run, only success_criteria can change, and a new one stops the run."""
    path = make_config(iterations=50)
    client = client_for(path)
    client.post("/api/build")
    config = client.post("/api/run").json()["config"]

    locked = json.loads(json.dumps(config))
    locked["pumps"][0]["bounds"] = [20, 100]
    response = client.put("/api/config", json=locked)
    assert response.status_code == 409
    assert "pumps" in response.json()["detail"]

    config["success_criteria"] = {
        "min_correlation": 0.9,
        "max_fwhm": None,
        "min_plqy": None,
        "poll_interval": 0.1,
    }
    state = client.put("/api/config", json=config).json()
    assert state["editable"] == ["success_criteria"]
    saved = Path(state["config_path"])
    assert (
        saved != path
        and json.loads(saved.read_text())["success_criteria"]
        == (config["success_criteria"])
    )

    # The watcher picks up the new criteria mid-run and stops well short of 50.
    state = wait_for(client, {"stopped", "finished", "failed"})
    assert state["status"] == "stopped", state["error"]
    assert 1 <= len(state["trials"]) < 50


def test_configs_load_from_the_gui_within_the_config_dir(
    tmp_path: Path, make_config, client_for
) -> None:
    """Start empty, list and load configs, and refuse paths outside the directory."""
    path = make_config(iterations=2)
    client = client_for()
    state = client.get("/api/state").json()
    assert (state["status"], state["config"]) == ("empty", None)
    assert client.post("/api/build").status_code == 409

    assert client.get("/api/configs").json() == ["halide.json"]
    state = client.post("/api/load", json={"path": "halide.json"}).json()
    assert (state["status"], state["config_path"]) == ("loaded", str(path.resolve()))

    outside = tmp_path.parent / "outside.json"
    for bad in ("../outside.json", str(outside), "Wanted.gr"):
        assert client.post("/api/load", json={"path": bad}).status_code == 403

    (tmp_path / "broken.json").write_text("{")
    response = client.post("/api/load", json={"path": "broken.json"})
    assert response.status_code == 422
    assert client.get("/api/state").json()["config_path"] == str(path.resolve())


def test_lock_file_allows_one_holder(tmp_path: Path) -> None:
    """A second lock on the same file exits with the first holder's details."""
    lock_path = tmp_path / ".xpd-autonomous.lock"
    held = acquire_lock(lock_path, url="http://first")
    with pytest.raises(SystemExit, match="http://first"):
        acquire_lock(lock_path, url="http://second")
    held.close()  # released, as when the first process exits
    acquire_lock(lock_path, url="http://second").close()


def test_n_points_other_than_one_is_rejected(make_config, client_for) -> None:
    """The acquisition plans take one suggestion per iteration: refuse n_points 2."""
    client = client_for(make_config(iterations=2))
    config = client.get("/api/state").json()["config"]
    config["run"]["n_points"] = 2
    response = client.put("/api/config", json=config)
    assert response.status_code == 422
    assert "n_points must be 1" in response.json()["detail"]
    assert client.get("/api/state").json()["config"]["run"]["n_points"] == 1
