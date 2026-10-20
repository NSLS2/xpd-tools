"""Helpers for PDF periodic."""
import os
import re

from diffpy.structure import Structure as DiffpyStructure
from pymatgen.core import Lattice
from pymatgen.core import Structure as PymatgenStructure


def structure_path(base_dir: str, src: str, ref: str) -> str:
    """CIF a dictionary entry is read from a CIF file or the MP structure."""
    if src == "cif":
        return ref
    return os.path.join(base_dir, "structures", f"{src}_{ref}.cif")


def _diffpy_to_pymatgen(struct: DiffpyStructure) -> PymatgenStructure:
    """Build a pymatgen Structure from a diffpy one."""
    lat = struct.lattice
    lattice = Lattice.from_parameters(
        lat.a, lat.b, lat.c,
        lat.alpha, lat.beta, lat.gamma
    )
    species = [re.sub(r"[0-9+\-]", "", atom.element) for atom in struct]
    frac_coords = [atom.xyz for atom in struct]
    return PymatgenStructure(lattice, species, frac_coords)
