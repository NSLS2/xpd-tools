"""diffpy.pdffit2-based structural refinement.

Everything that touches diffpy.pdffit2 lives in this package
so deprecating pdffit2 is easier.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import numpy as np

from xpd_tools.optimization.analysis import pdf_profile
from xpd_tools.optimization.scoring import EnsembleScorers, _resolve_scorer

_PDF_QMAX = 18.0
_PDF_RMAX = 120.0
_PDF_RMIN = 2.5
_PDF_QDAMP = 0.031
_PDF_QBROAD = 0.032
_PDF_TOLERANCE = 1e-6


def fit_pdf_correlations(
    phases: Sequence[Any],
    pdf_data: Mapping[str, np.ndarray],
    *,
    r_min: float = 2.0,
    r_max: float = 20.0,
    ensemble_scorers: EnsembleScorers,
) -> dict[str, float]:
    """Refine and correlate each configured phase using pdffit2."""
    from diffpy.pdffit2 import PdfFit
    from diffpy.structure import loadStructure

    experimental_r = np.asarray(pdf_data["gr_r"], dtype=float)
    experimental_g = np.asarray(pdf_data["gr_G"], dtype=float)
    finite = np.isfinite(experimental_r) & np.isfinite(experimental_g)
    if np.count_nonzero(finite) < 2:
        raise ValueError("not enough finite PDF points to fit G(r)")

    fit_rmax = min(_PDF_RMAX, float(np.max(experimental_r[finite])))
    if fit_rmax <= _PDF_RMIN:
        raise ValueError(
            f"PDF fit rmax must be greater than {_PDF_RMIN} A; "
            f"measured rmax is {fit_rmax}"
        )
    results: dict[str, float] = {}
    with TemporaryDirectory() as directory:
        output_directory = Path(directory)
        measured_path = output_directory / "measured.gr"
        np.savetxt(
            measured_path,
            np.column_stack((experimental_r[finite], experimental_g[finite])),
            fmt="%.10g %.10g",
        )
        for phase in phases:
            # cnn-scored phases have no PDFfit2-refined curve to score --
            # they're excluded here the same way _raw_pdf_correlations
            # excludes them from its own reference-.gr loop (see CnnScorer).
            if phase.scoring_function == "cnn":
                continue
            if phase.cif_path is None:
                raise RuntimeError(f"phase {phase.name!r} has no CIF path")
            clean_cif = _write_oxidation_free_cif(phase.cif_path, output_directory)
            structure = loadStructure(str(clean_cif))
            structure.Uisoequiv = 0.04
            structure.title = clean_cif.stem

            pdf_fit: Any = PdfFit()
            pdf_fit.read_data(
                str(measured_path),
                "X",
                _PDF_QMAX,
                _PDF_QDAMP,
            )
            pdf_fit.add_structure(structure)
            if phase.constraint_profile == "cs_pb_br3":
                _set_cs_pb_br3_constraints(pdf_fit)
            pdf_fit.constrain(pdf_fit.dscale, "@902")
            pdf_fit.setpar(902, 1.0)
            pdf_fit.setvar(pdf_fit.qdamp, _PDF_QDAMP)
            pdf_fit.setvar(pdf_fit.qbroad, _PDF_QBROAD)
            pdf_fit.pdfrange(1, _PDF_RMIN, fit_rmax)
            pdf_fit.refine(toler=_PDF_TOLERANCE)
            results[f"pdf_fit_corr_{phase.name}"] = pdf_profile(
                experimental_r,
                experimental_g,
                np.asarray(pdf_fit.getR()),
                np.asarray(pdf_fit.getpdf_fit()),
                r_min=r_min,
                r_max=r_max,
                function=_resolve_scorer(
                    phase.scoring_function, phase.name, ensemble_scorers
                ),
            )
    return results


def _set_cs_pb_br3_constraints(pdf_fit: Any, *, phase_index: int = 1) -> None:
    """Apply the established CsPbBr3 pdffit2 constraints."""
    pdf_fit.setphase(phase_index)
    for axis, parameter in enumerate((11, 12, 13), start=1):
        pdf_fit.constrain(pdf_fit.lat(axis), f"@{parameter}")
        pdf_fit.setpar(parameter, pdf_fit.lat(axis))

    pdf_fit.constrain("pscale", "@111")
    pdf_fit.setpar(111, 1.0)
    pdf_fit.constrain(pdf_fit.delta2, "@122")
    pdf_fit.setpar(122, 6.87)
    pdf_fit.fixpar(122)
    pdf_fit.constrain(pdf_fit.spdiameter, "@133")
    pdf_fit.setpar(133, 80)

    for atom_range, parameter, value in (
        (range(1, 5), 101, 0.029385),
        (range(5, 9), 102, 0.027296),
        (range(9, 17), 103, 0.041577),
        (range(17, 21), 104, 0.028164),
    ):
        for atom_index in atom_range:
            pdf_fit.constrain(pdf_fit.u11(atom_index), f"@{parameter}")
            pdf_fit.constrain(pdf_fit.u22(atom_index), f"@{parameter}")
            pdf_fit.constrain(pdf_fit.u33(atom_index), f"@{parameter}")
        pdf_fit.setpar(parameter, value)
        pdf_fit.fixpar(parameter)


def _write_oxidation_free_cif(cif_path: Path, output_directory: Path) -> Path:
    """Write a temporary CIF without oxidation states for diffpy."""
    from pymatgen.io.cif import CifParser, CifWriter

    structure = CifParser(str(cif_path)).parse_structures(primitive=True)[0]
    structure.remove_oxidation_states()
    output_path = output_directory / f"{cif_path.stem}_pym.cif"
    CifWriter(structure, symprec=0.1).write_file(str(output_path))
    return output_path
