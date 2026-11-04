"""Low syringe volume: the campaign waits in 'refill' until POST /api/refilled."""

import json

from fastapi.testclient import TestClient

from xpd_autonomous_gui.server import Session, _simulation_options, create_app


def test_refill_then_continue(make_config, tmp_path, wait_for) -> None:
    """A short syringe stops the campaign; Refilled clears its counter and finishes it."""
    path = make_config(iterations=3)
    config = json.loads(path.read_text())
    config["experiment"]["sources"][0]["loaded_ml"] = 3.0  # 2 mL usable (1 mL reserve)
    path.write_text(json.dumps(config))
    session = Session(tmp_path, path)
    client = TestClient(create_app(session))

    client.post("/api/build")
    pump = session.stopper.devices["dds2_p1"]
    pump.read_infused.put(2.0)  # already used: nothing left for a trial
    client.post("/api/run")

    state = wait_for(client, {"refill", "failed", "finished"})
    assert state["status"] == "refill", state["error"]
    assert state["refill"]["pumps"] == ["dds2_p1"]
    assert state["refill"]["message"].startswith("Refill needed: dds2_p1 (CsPb) has 0.00 mL")
    assert state["remaining"] == 3
    # Run would start over; the refill has to be acknowledged (or cleared) first.
    assert client.post("/api/run").status_code == 409

    assert client.post("/api/refilled").json()["status"] == "running"
    assert pump.read_infused.get() < 2.0  # counter cleared, then counting again
    state = wait_for(client, {"refill", "failed", "finished"})
    assert state["status"] == "finished", state["error"]
    completed = [t for t in state["trials"] if t["trial_status"] == "COMPLETED"]
    assert len(completed) == 3


def test_refilled_needs_a_pending_refill(make_config, client_for) -> None:
    """Refilled is refused unless a refill is pending."""
    client = client_for(make_config(iterations=1))
    assert client.post("/api/refilled").status_code == 409


def test_old_config_shows_loaded_ml(make_config, client_for) -> None:
    """A config written before loaded_ml existed gets it (blank) for the Config page."""
    path = make_config(iterations=1)
    config = json.loads(path.read_text())
    del config["experiment"]["sources"][0]["loaded_ml"]
    del config["experiment"]["sources"][0]["reserve_ml"]
    path.write_text(json.dumps(config))

    source = client_for(path).get("/api/state").json()["config"]["experiment"]["sources"][0]
    assert (source["loaded_ml"], source["reserve_ml"]) == (None, 1.0)


def test_state_lists_fixed_choices(make_config, client_for) -> None:
    """Objective functions and screening modes come from xpd-tools, for dropdowns."""
    choices = client_for(make_config(iterations=1)).get("/api/state").json()["choices"]
    assert "pearson" in choices["xray.objective_function"]
    assert choices["evaluation_method"] == ["uvvis", "xray", "xray-uvvis"]
    assert choices["pdf_mode"] == ["raw", "fit", "raw_tracked"]
    assert choices["xray.screening"] == ["unscreened", "screen_only", "screen_and_record"]


def test_simulation_presets_come_from_the_config(make_config, client_for) -> None:
    """run.local.simulated: fake or Materials Project, with a DOF dropdown per phase."""
    client = client_for(make_config(iterations=1))
    state = client.get("/api/state").json()
    fake, mp = state["presets"]["run.local.simulated"]
    assert fake["value"] is None
    assert mp["value"] == {"dof_for_phase": {"Wanted": None}}
    assert state["choices"]["run.local.simulated.dof_for_phase.Wanted"] == [
        None,
        "infusion_rate_CsPb",
    ]

    # Materials Project with no DOF picked can't simulate anything: Build says so.
    config = state["config"]
    config["run"]["local"]["simulated"] = mp["value"]
    assert client.put("/api/config", json=config).status_code == 200
    response = client.post("/api/build")
    assert response.status_code == 500
    assert "pick a DOF for at least one phase" in response.json()["detail"]


def test_uvvis_only_simulation_uses_the_configs_dof_for_phase() -> None:
    """A UV-Vis-only config has no xray phases: the DOF dropdowns are for the
    phases already named in run.local.simulated.dof_for_phase."""
    simulated = {"dof_for_phase": {"CsPbBr3": "infusion_rate_Br", "CsPbI3": None}}
    config = {
        "evaluation_method": "uvvis",
        "experiment": {"sources": [{"dof": "infusion_rate_Br"}, {"dof": "infusion_rate_I"}]},
        "run": {"local": {"simulated": simulated}},
    }
    presets, choices = _simulation_options(config)
    assert presets["run.local.simulated"][1]["value"] == {
        "dof_for_phase": {"CsPbBr3": None, "CsPbI3": None}
    }
    dofs = [None, "infusion_rate_Br", "infusion_rate_I"]
    assert choices == {
        "run.local.simulated.dof_for_phase.CsPbBr3": dofs,
        "run.local.simulated.dof_for_phase.CsPbI3": dofs,
    }


def test_success_criteria_options_apply(make_config, client_for) -> None:
    """The three success-criteria options; a picked one applies with only its fields."""
    client = client_for(make_config(iterations=1))
    state = client.get("/api/state").json()
    labels = [option["label"] for option in state["presets"]["success_criteria"]]
    assert labels == [
        "None (runs all iterations)",
        "Minimum correlation",
        "Maximum FWHM and minimum PLQY",
    ]

    config = {**state["config"], "success_criteria": {"min_correlation": 0.8, "poll_interval": 5}}
    applied = client.put("/api/config", json=config)
    assert applied.status_code == 200, applied.json()
    assert client.post("/api/build").status_code == 200

    # Picked but not filled in: Apply says what's missing.
    config["success_criteria"] = {"min_correlation": None, "poll_interval": 5}
    refused = client.put("/api/config", json=config)
    assert refused.status_code == 422
    assert "at least one real target" in refused.json()["detail"]


def test_rebuild_drops_a_pending_refill(make_config, tmp_path, wait_for) -> None:
    """A refill belongs to the agent that hit it: Build (or Load/Apply) starts clean."""
    path = make_config(iterations=2)
    config = json.loads(path.read_text())
    config["experiment"]["sources"][0]["loaded_ml"] = 3.0
    path.write_text(json.dumps(config))
    session = Session(tmp_path, path)
    client = TestClient(create_app(session))

    client.post("/api/build")
    session.stopper.devices["dds2_p1"].read_infused.put(2.0)
    client.post("/api/run")
    assert wait_for(client, {"refill", "failed", "finished"})["status"] == "refill"

    rebuilt = client.post("/api/build").json()  # fresh devices: counters at 0
    assert (rebuilt["status"], rebuilt["refill"], rebuilt["remaining"]) == ("built", None, None)
    assert client.post("/api/run").status_code == 200
    assert wait_for(client, {"refill", "failed", "finished"})["status"] == "finished"


def test_state_says_building_while_build_runs(make_config, client_for, monkeypatch) -> None:
    """Other pages and tabs see a Build in progress (actions wait for it)."""
    import threading

    from xpd_autonomous_gui import server

    release, entered = threading.Event(), threading.Event()
    real = server._build_local

    def slow_build_local(*args, **kwargs):
        entered.set()
        release.wait(10)
        return real(*args, **kwargs)

    monkeypatch.setattr(server, "_build_local", slow_build_local)
    client = client_for(make_config(iterations=1))
    thread = threading.Thread(target=lambda: client.post("/api/build"))
    thread.start()
    assert entered.wait(10)
    assert client.get("/api/state").json()["building"] is True
    release.set()
    thread.join(30)
    assert client.get("/api/state").json()["building"] is False


def test_queue_server_refill_clears_counters_there(make_config, tmp_path) -> None:
    """Queue-server mode: blop's error text is recognised; Refilled asks the worker to clear."""
    from concurrent.futures import Future
    from unittest.mock import MagicMock, patch

    import pandas as pd

    session = Session(tmp_path, make_config(iterations=3))
    session.build_agent.queue_server = True
    futures: list[Future] = []

    def run(iterations, n_points):
        futures.append(Future())
        return futures[-1]

    agent = MagicMock()
    agent.run.side_effect = run
    agent.ax_client.summarize.return_value = pd.DataFrame({"trial_status": ["COMPLETED"]})
    session.agent = session.stopper = agent
    session.status = "built"
    session.run()

    # As blop's QueueserverOptimizationRunner reports a failed acquisition run.
    futures[0].set_exception(RuntimeError(
        "Acquisition run 'abc' ended with status 'fail': RefillRequired('Refill needed: "
        "dds2_p1 (CsPb) has 0.10 mL left, next trial needs ~0.34 mL. pumps to refill: dds2_p1')"
    ))
    assert session.status == "refill"
    assert session.refill["pumps"] == ["dds2_p1"]
    assert session.refill["message"].startswith("Refill needed: dds2_p1 (CsPb) has 0.10 mL")

    with patch("bluesky_queueserver_api.http.REManagerAPI") as api_class:
        session.refilled()
    plan = api_class.return_value.item_execute.call_args.args[0]
    assert (plan.to_dict()["name"], plan.to_dict()["args"]) == ("clear_infused_volumes", [["dds2_p1"]])
    api_class.return_value.wait_for_idle.assert_called_once()
    assert agent.run.call_args.kwargs["iterations"] == 3  # nothing completed yet
    assert session.status == "running"
