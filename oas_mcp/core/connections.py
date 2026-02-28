"""
OpenMDAO connection helpers for aero and aerostruct models.

Centralises the verbose connect() calls so builders.py stays readable.
"""

import openmdao.api as om


def connect_aero_surface(model: om.Group, name: str, point_name: str) -> None:
    """
    Connect a Geometry group output to an AeroPoint.

    Parameters
    ----------
    model : om.Group
        Top-level model group.
    name : str
        Surface name (matches Geometry subsystem name).
    point_name : str
        AeroPoint subsystem name (e.g. "aero").
    """
    # mesh → def_mesh in the aero point and in aero_states
    model.connect(name + ".mesh", point_name + "." + name + ".def_mesh")
    model.connect(name + ".mesh", point_name + ".aero_states." + name + "_def_mesh")
    # t_over_c → perf component
    model.connect(name + ".t_over_c", point_name + "." + name + "_perf.t_over_c")


def connect_aerostruct_surface(model: om.Group, name: str, point_name: str) -> None:
    """
    Connect an AerostructGeometry group to an AerostructPoint.

    Parameters
    ----------
    model : om.Group
        Top-level model group.
    name : str
        Surface name (matches AerostructGeometry subsystem name).
    point_name : str
        AerostructPoint subsystem name (e.g. "AS_point_0").
    """
    com_name = point_name + "." + name + "_perf"

    # Structural stiffness and nodes → coupled group
    model.connect(name + ".local_stiff_transformed", point_name + ".coupled." + name + ".local_stiff_transformed")
    model.connect(name + ".nodes", point_name + ".coupled." + name + ".nodes")

    # Mesh → coupled group
    model.connect(name + ".mesh", point_name + ".coupled." + name + ".mesh")

    # Performance calculation variables
    model.connect(name + ".radius", com_name + ".radius")
    model.connect(name + ".thickness", com_name + ".thickness")
    model.connect(name + ".nodes", com_name + ".nodes")
    model.connect(name + ".cg_location", point_name + ".total_perf." + name + "_cg_location")
    model.connect(name + ".structural_mass", point_name + ".total_perf." + name + "_structural_mass")
    model.connect(name + ".t_over_c", com_name + ".t_over_c")
