# Autonomous GUI

Run an autonomous campaign from a browser instead of a notebook.

To use the GUI, the web page needs to be built and the python server installed.

From xpd-tools/autonomous-gui/web
```
npm install 
npm run build
```

1. Develop and validate the workflow in a notebook, then save it:
   `build_agent.to_config("configs/halide.json")`.
2. Set how it runs with `build_agent.set_run(...)` before saving: `to_config()` writes it as the
   JSON's `run` section (below).
3. On the day, serve it and open the printed URL:

```bash
xpd-autonomous-server configs/halide.json          # http://127.0.0.1:8765
xpd-autonomous-server --config-dir configs         # start empty, pick in the GUI
```

### One driver at a time

The server takes an exclusive lock on `.xpd-autonomous.lock` in the working directory
(`--lock-file` to change it) and refuses to start if another server holds it.


## Pages

- **Run**: Load, build, and run a config.
- **Config**: Modify the config. Large-scale changes should just be done in a ipynb.
- **Trials**: Plotting and monitoring of the run.
