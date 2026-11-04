"""Visualization helpers for halide perovskite UV-Vis optimization campaigns.

Backing module — import and call from the notebook or scripts.  Requires
``matplotlib``.

Typical import::

    from xpd_tools.optimization.visualization import plot_bandgap_wavelength
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    import matplotlib.pyplot as plt
    import pandas as pd

HC_EV_NM = 1239.841984  # Planck's constant × c, in eV·nm


# ---------------------------------------------------------------------------
# Band-gap ↔ wavelength conversion utilities
# ---------------------------------------------------------------------------

def wavelength_nm(bandgap_ev: float | np.ndarray) -> float | np.ndarray:
    """Convert photon energy / optical band gap in eV to wavelength in nm."""
    return HC_EV_NM / np.asarray(bandgap_ev)


def bandgap_ev(wavelength: float | np.ndarray) -> float | np.ndarray:
    """Convert wavelength in nm to photon energy in eV.

    Returns ``inf`` for zero wavelength (physically correct limit; matplotlib
    may query the inverse transform at zero while laying out secondary axes).
    """
    with np.errstate(divide="ignore", invalid="ignore"):
        return HC_EV_NM / np.asarray(wavelength)


def wavelength_color(wavelength: float) -> str:
    """Approximate visible-light hex colour; UV and IR are deliberately pale."""
    if wavelength < 380:
        return "#eadcff"   # ultraviolet
    if wavelength < 440:
        return "#6c5ce7"
    if wavelength < 490:
        return "#1687d4"
    if wavelength < 510:
        return "#16b6a0"
    if wavelength < 580:
        return "#48b749"
    if wavelength < 645:
        return "#f2ce31"
    if wavelength <= 700:
        return "#f07824"
    return "#ffd6cf"       # infrared


# ---------------------------------------------------------------------------
# Main plot
# ---------------------------------------------------------------------------

def plot_bandgap_wavelength(
    df: pd.DataFrame,
    *,
    e_min: float = 1.4,
    e_max: float = 4.2,
    save: str | None = None,
    references: list[tuple[float, str]] | None = None,
) -> plt.Figure:
    """Plot trial peak wavelengths on a band-gap (eV) vs trial-index axis.

    Each trial is coloured by the approximate visible colour corresponding to
    its fitted PL peak, and fades in opacity from first to last trial.

    Parameters
    ----------
    df:
        DataFrame from ``summarize_campaign()`` (or ``ax_client.summarize()``).
        Must contain ``trial_index`` and ``Peak`` columns.
    e_min, e_max:
        Y-axis limits in eV.
    save:
        If given, write the figure to this path.
    references:
        List of ``(bandgap_ev, label)`` tuples for horizontal reference lines.
        Defaults to the three canonical CsPb halide phases.

    Returns
    -------
    matplotlib.figure.Figure

    Example (notebook cell)::

        from xpd_tools.optimization.analysis import plot_bandgap_wavelength
        from xpd_tools.optimization.campaign import summarize_campaign

        df = summarize_campaign(run_agent)
        plot_bandgap_wavelength(df, save="bandgap.png")
    """
    import matplotlib.pyplot as plt

    if references is None:
        references = [
            (2.29, "CsPbBr₃"),
            (1.63, "CsPbI₃"),
            (2.90, "CsPbCl₃"),
        ]

    fig, ax = plt.subplots(figsize=(7.2, 6.2), constrained_layout=True)

    # Colour strip — position is driven by wavelength, not a linear nm scale.
    wavelengths = np.linspace(wavelength_nm(e_max), wavelength_nm(e_min), 500)
    for lower, upper in zip(wavelengths[:-1], wavelengths[1:], strict=False):
        mid = (lower + upper) / 2
        ax.axhspan(
            bandgap_ev(upper), bandgap_ev(lower),
            color=wavelength_color(mid), alpha=0.33, linewidth=0, zorder=0,
        )

    index = df["trial_index"].to_numpy()
    peaks = df["Peak"].to_numpy()
    ind_max = max(index.max(), 1)
    for ind, peak in zip(index, peaks, strict=False):
        ax.scatter(
            ind / ind_max,
            bandgap_ev(peak),
            color=wavelength_color(peak),
            alpha=ind / ind_max,
            linewidth=2,
        )

    for energy, label in references:
        color = wavelength_color(wavelength_nm(energy))
        ax.axhline(energy, color=color, linewidth=2)
        ax.text(0.03, energy + 0.035, label, color=color, weight="bold")

    ax.text(0.5, e_max - 0.15, "ultraviolet",
        ha="center",
        color="#6a1b9a",
        weight="bold"
    )
    ax.text(0.5, e_min + 0.08, "infrared",
        ha="center",
        color="#b71c1c",
        weight="bold"
    )

    ax.set_xlim(0, 1)
    ax.set_xticks([])
    ax.set_ylim(e_min, e_max)
    ax.set_ylabel("Band gap / photon energy, $E$ (eV)")
    ax.set_title("Trial PL peaks — band gap ↔ wavelength")
    ax.grid(axis="y", color="white", linewidth=1.2)

    right = ax.secondary_yaxis("right", functions=(wavelength_nm, bandgap_ev))
    right.set_ylabel("Wavelength, $\\lambda$ (nm)")
    right.set_yticks([300, 350, 400, 450, 500, 520, 550, 600, 650, 700, 750, 800])

    if save:
        fig.savefig(save, dpi=170)
        print(f"Figure saved → {save}")

    return fig
