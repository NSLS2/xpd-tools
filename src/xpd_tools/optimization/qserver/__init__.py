"""XPD Queue Server plans for TJ Barrett's autonomous workflow, ported from the profile.

``xray_uvvis.py`` and ``acquire_variants.py`` are verbatim copies of
``startup/97-xray_uvvis.py`` and ``startup/98-xpd-tools.py`` from the ``qserver``
branch of NSLS2/xpd-profile-collection (commit 12397de). They still use devices and
helpers that the profile defines, so the profile calls::

    from xpd_tools.optimization.qserver import install

    install(globals())

which hands those names to the copied code and puts every name the two startup
files used to define back into the profile namespace, where the Queue Server finds
the plans (``xray_uvvis_acquire``, ``uvvis_acquire``, ``xray_acquire``).

This is a first step. ``PROFILE_NAMES`` lists what the copied code takes from the
profile by name in its source; follow-up PRs turn those into explicit arguments.
The copied code also looks devices up by name at run time
(``_resolve_pumps``: ``globals().get("dds2_p1")``), which in the profile found any
profile name, so ``install`` gives the modules every other profile name as well.
"""

from collections.abc import MutableMapping
from typing import Any

import bluesky.plan_stubs as bps
import bluesky.preprocessors as bpp

from xpd_tools.optimization.qserver import acquire_variants, xray_uvvis

#: Names the copied code uses from the profile namespace when a plan runs.
PROFILE_NAMES = (
    # Devices
    "pe1c",  # 80-areadetector.py
    "qepro",  # 25-QEPro.py
    "LED",  # 28-Lights_shutter.py
    "UV_shutter",  # 28-Lights_shutter.py
    "fs",  # 10-motors.py
    # Pump flow helpers, 29-flow_plan.py
    "start_group_infuse",
    "stop_group",
    "set_group_infuse2",
    "sleep_sec_q",
    "wait_equilibrium2",
    # Dark frames and run metadata, 94-CHL-plans.py (xpdacq)
    "periodic_dark",
    "_pre_plan",
    "_inject_analysis_stage",
    "_inject_calibration_md",
    "_inject_qualified_dark_frame_uid",
)

#: Names startup/97-xray_uvvis.py defined in the profile namespace.
XRAY_UVVIS_NAMES = (
    "FlowConfig",
    "XrayConfig",
    "WashConfig",
    "QualityConfig",
    "DEFAULT_FLOW_CONFIG",
    "DEFAULT_XRAY_CONFIG",
    "DEFAULT_WASH_CONFIG",
    "DEFAULT_QUALITY_CONFIG",
    "DEFAULT_THRESHOLDS",
    "_Q_BATCH_INDEX",
    "_Q_VERDICT",
    "_Q_PEAK_WL",
    "_Q_N_GOOD",
    "_Q_N_BAD",
    "_Q_N_EVENTS",
    "_Q_SIGS",
    "_find_nearest_idx",
    "_classify_pl",
    "PLQualityMonitor",
    "measure_absorbance",
    "measure_pl",
    "measure_scattering",
    "_emit_quality_event",
    "_pl_with_quality_gate",
    "_wash_loop",
    "steady_state_flow",
    "_BLOP_CORR_KEY",
    "_strip_blop_uid_from_dark",
    "xray_uvvis_acquire",
    "_make_sample_name",
    "_resolve_pumps",
    "_resolve_pumps_from_dofs",
)

#: Names startup/98-xpd-tools.py defined in the profile namespace.
ACQUIRE_VARIANTS_NAMES = ("uvvis_acquire", "xray_acquire")


def install(namespace: MutableMapping[str, Any]) -> None:
    """Hand the profile's names to the copied plans and export the plans.

    Parameters
    ----------
    namespace : MutableMapping[str, Any]
        The profile namespace, ``globals()`` in a startup file.

    Raises
    ------
    NameError
        If the profile does not define a name the copied code uses.
    """
    missing = [name for name in PROFILE_NAMES if name not in namespace]
    if missing:
        msg = f"The profile does not define {', '.join(missing)}."
        raise NameError(msg)
    profile = {name: namespace[name] for name in PROFILE_NAMES}

    for module in (xray_uvvis, acquire_variants):
        module_vars = vars(module)
        module_vars.update(profile)
        # Run-time look-ups by name (e.g. pumps in _resolve_pumps) used to see the
        # whole profile namespace; give them everything the profile has now.
        for name, value in namespace.items():
            if not name.startswith("__"):
                module_vars.setdefault(name, value)
    # 98 relied on 97's names and on bps/bpp from the profile namespace.
    vars(acquire_variants).update(
        {name: getattr(xray_uvvis, name) for name in XRAY_UVVIS_NAMES},
        bps=bps,
        bpp=bpp,
    )

    namespace.update({name: getattr(xray_uvvis, name) for name in XRAY_UVVIS_NAMES})
    namespace.update(
        {name: getattr(acquire_variants, name) for name in ACQUIRE_VARIANTS_NAMES}
    )
