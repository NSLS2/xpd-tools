"""Materials Project API user."""

import os
from dataclasses import dataclass

import numpy as np
from mp_api.client import MPRester

GR_METHOD = "periodic"


@dataclass
class MPAPIUser:
    """Materials Project API user class, including loading, parsing, etc."""

    q: tuple
    r: tuple
    rstep: float = .01
    qstep: float = .01
    qdamp: float = 0.0
    material_ids: list[str] | None = None
    formulas: list[str] | None = None
    api_key: str | None = None
    key_name: str = "MP_API_KEY"

    def __post_init__(self):
        """Read API Key from the environment."""
        if self.api_key is None:
            self.api_key = os.environ.get(self.key_name)
            if not self.api_key:
                raise ValueError(
                    f"{self.key_name} isn't set: pass api_key, or add it to your "
                    "environment .env"
                )
        if self.material_ids is None and self.formulas is None:
            raise ValueError(
                "Either material_ids or formulas have to be specified"
            )

    def _pdf(self, structure):
        """Compute the PDF of a structure using the Debye equation."""
        if GR_METHOD == "periodic":
            from xpd_tools.optimization.utils.pdf_periodic import pdf_periodic

            r, gr = pdf_periodic(
                structure,
                rmin=self.r[0],
                rmax=self.r[1],
                rstep=self.rstep,
                qdamp=self.qdamp,
                u=0.04,
                qmin=self.q[0],
                qmax=self.q[1],
                pad=10.0,
                strain=None,
            )

        elif GR_METHOD == 'ezpit':
            import ezpit.core.processing as ezproc
            from ezpit.core.elem_tables import get_aff_scattering_factors
            from ezpit.core.io import group_atoms

            # Defense against modifying the input structure with supercell expansion
            structure = structure.copy()

            # Expand to a supercell that covers rmax first
            n_repeats = [max(1, int(np.ceil(2 * self.r[1] / length)))
                         for length in structure.lattice.abc]
            structure.make_supercell(n_repeats)

            # Populate arrays from the supercell structure
            atom_names = [site.specie.symbol for site in structure]
            atom_positions = np.array([site.coords for site in structure])
            atom_unique_names, _, atom_indices = group_atoms(atom_names)
            scattering_factors = get_aff_scattering_factors(atom_unique_names)
            atom_distance_matrix = ezproc.create_atom_distance_matrix(
                atom_positions)

            # Calculate Sq from the scattering factors and atom distance matrix
            q, _, sq, *_ = ezproc.cal_Sq(
                atom_indices, scattering_factors, atom_distance_matrix,
                qmin=self.q[0], qmax=self.q[1], qstep=self.qstep, return_Iq=True
            )

            # Calculate Gr from the Debye equation
            r, gr = ezproc.cal_Gr_integral(
                q, sq,
                rmin=self.r[0], rmax=self.r[1],
                rstep=self.rstep, qdamp=self.qdamp
            )

        return r, gr

    def _fetch(
        self,
        mpr: MPRester,
        *,
        formula: str | None = None,
        mid: str | None = None
    ):
        if formula is not None:
            docs = mpr.materials.summary.search(
                formula=formula, fields=["material_id", "energy_above_hull"]
            )
            mid = min(docs, key=lambda d: d.energy_above_hull).material_id
        return mpr.get_structure_by_material_id(mid)

    def load(self):
        """Load structures from the Materials Project API and compute their PDF."""
        with MPRester(self.api_key) as mpr:
            structs = [self._fetch(mpr, mid=m)
                       for m in (self.material_ids or [])]
            structs += [self._fetch(mpr, formula=f)
                        for f in (self.formulas or [])]

        # Compute the PDF of each structure and stack the results
        r, gs = None, []
        for s in structs:
            r, g = self._pdf(s)
            gs.append(g)
        return r, np.column_stack(gs)

    def band_gaps(self) -> dict[str, float]:
        """Look up each formula's DFT band gap (eV) from Materials Project.

        Sorts and determines lowest energy above hull, and extracts band gap
        of that materials project entry.
        """
        with MPRester(self.api_key) as mpr:
            gaps = {}
            for formula in self.formulas or []:
                docs = mpr.materials.summary.search(
                    formula=formula,
                    fields=["material_id", "energy_above_hull", "band_gap"],
                )
                # Take the bandgap of the lowest energy above hull
                gaps[formula] = min(docs, key=lambda d: d.energy_above_hull).band_gap
            return gaps
