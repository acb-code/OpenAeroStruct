"""Default values for OAS surface dicts and flight conditions."""

import numpy as np
from openaerostruct.utils.constants import grav_constant

# Default flight conditions for aero analysis
DEFAULT_AERO_CONDITIONS = {
    "velocity": 248.136,  # m/s (~Mach 0.84 at cruise alt)
    "alpha": 5.0,  # deg
    "Mach_number": 0.84,
    "reynolds_number": 1.0e6,  # 1/m
    "density": 0.38,  # kg/m^3
    "cg": [0.0, 0.0, 0.0],  # m
}

# Default extra conditions for aerostruct
DEFAULT_AEROSTRUCT_CONDITIONS = {
    **DEFAULT_AERO_CONDITIONS,
    "CT": grav_constant * 17.0e-6,  # 1/s (specific fuel consumption)
    "R": 11.165e6,  # m (range)
    "W0": 0.4 * 3e5,  # kg (empty weight)
    "speed_of_sound": 295.4,  # m/s
    "load_factor": 1.0,
    "empty_cg": [0.0, 0.0, 0.0],  # m
}

# Default mesh parameters
DEFAULT_MESH_PARAMS = {
    "num_x": 2,
    "num_y": 7,
    "wing_type": "rect",
    "symmetry": True,
    "span": 10.0,
    "root_chord": 1.0,
}

# Default surface properties (aero only)
DEFAULT_AERO_SURFACE = {
    "S_ref_type": "wetted",
    "CL0": 0.0,
    "CD0": 0.015,
    "k_lam": 0.05,
    "t_over_c_cp": np.array([0.15]),
    "c_max_t": 0.303,
    "with_viscous": True,
    "with_wave": False,
}

# Default structural properties (aluminum 7075)
DEFAULT_STRUCT_PROPS = {
    "fem_model_type": "tube",
    "E": 70.0e9,  # Pa
    "G": 30.0e9,  # Pa
    "yield": 500.0e6,  # Pa
    "safety_factor": 2.5,
    "mrho": 3.0e3,  # kg/m^3
    "fem_origin": 0.35,
    "wing_weight_ratio": 2.0,
    "struct_weight_relief": False,
    "distributed_fuel_weight": False,
    "exact_failure_constraint": False,
}
