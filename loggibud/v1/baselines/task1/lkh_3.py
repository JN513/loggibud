"""Implements the Lin-Kernighan-Helsgaun (LKH) solver
The solver used here is the LKH-3 version [1], which is able to solve CVRP
instances.

References
----------
    [1] https://github.com/cerebis/LKH3
"""
import logging
import os
import subprocess
from dataclasses import dataclass
from itertools import groupby
from math import ceil
from typing import Dict, List, Optional

import lkh
import numpy as np

from loggibud.v1.types import (
    CVRPInstance,
    CVRPSolution,
    CVRPSolutionVehicle,
    JSONDataclassMixin,
)
from loggibud.v1.distances import OSRMConfig
from loggibud.v1.data_conversion import to_tsplib, TSPLIBConversionParams


logger = logging.getLogger(__name__)


@dataclass
class LKHParams(JSONDataclassMixin):

    time_limit_s: int = 60
    """Time limit in seconds to step the solver."""

    num_runs: int = 1
    """Number of runs (as in a multistart heuristic)."""

    osrm_config: Optional[OSRMConfig] = None
    """Config for calling OSRM distance service."""


def solve(
    instance: CVRPInstance, params: Optional[LKHParams] = None
) -> CVRPSolution:
    """Solve a CVRP instance using LKH-3"""

    params = params or LKHParams()

    conversion_params = TSPLIBConversionParams(osrm_config=params.osrm_config)
    tsplib_instance = to_tsplib(instance, conversion_params)

    # LKH solution params, for details check the LKH documentation.
    lkh_params = dict(
        mtsp_objective="MINSUM",
        runs=params.num_runs,
        time_limit=params.time_limit_s,
        vehicles=_get_num_vehicles(instance),
    )

    current_path = os.path.dirname(os.path.abspath(__file__))
    lkh_solution = lkh.solve(
        f"{current_path}/LKH", tsplib_instance, **lkh_params
    )

    solution = _unwrap_lkh_solution(instance, lkh_solution)

    return solution


def _unwrap_lkh_solution(
    instance: CVRPInstance, lkh_solution: List[int]
) -> CVRPSolution:
    """Read the files generated by the solver

    The output is stored in a TSPLIB-like format. Here is a typical example.

    Suppose a problem with depot at node 1 and deliveries at 2, 3, 4, 5 and 6.
    Now, suppose the solution has two routes such as:
        - Route 1: [1, 2, 3]
        - Route 2: [1, 4, 5, 6]

    The output would be written as a sequence like:
        1
        2
        3
        7 <---
        4
        5
        6


    The first node is 1, the depot, and the following are deliveries in the
    first route. Then, we reach a node 7, which is greater than all nodes in
    the problem. This actually marks the start of another route, and if we had
    more routes, it would be split with an 8, and so on.

    The reading goes on until a -1 is obtained, thus marking the end of all
    routes.
    """

    num_deliveries = len(instance.deliveries)

    # To retrieve the delivery indices, we have to subtract two, that is the
    # same as ignoring the depot and reindexing from zero.
    delivery_indices = np.array(lkh_solution[0]) - 2

    # Now we split the sequence into vehicles using a simple generator.
    def route_gen(seq):
        route = []

        for el in seq[1:]:
            if el < num_deliveries:
                route.append(el)

            elif route:
                yield np.array(route)
                route = []

    delivery_indices = list(route_gen(delivery_indices))

    # To enable multi-integer indexing, we convert the deliveries into an
    # object np.array.
    np_deliveries = np.array(instance.deliveries, dtype=object)

    def build_vehicle(route_delivery_indices):
        deliveries = np_deliveries[route_delivery_indices]

        return CVRPSolutionVehicle(
            origin=instance.origin, deliveries=deliveries.tolist()
        )

    routes = [build_vehicle(indices) for indices in delivery_indices]

    return CVRPSolution(name=instance.name, vehicles=routes)


def _get_num_vehicles(instance: CVRPInstance) -> int:
    """Estimate a proper number of vehicles for an instance
    The typical number of vehicles used internally by the LKH-3 is given by

        ceil(total_demand / vehicle_capacity)

    Unfortunately, this does not work in some cases. Here is a simple example.
    Consider three deliveries with demands 3, 4 and 5, and assume the vehicle
    capacity is 6. The total demand is 12, so according to this equation we
    would require ceil(12 / 6) = 2 vehicles.
    Unfortunately, there is no way to place all three deliveries in these two
    vehicles without splitting a package.

    Thus, we use a workaround by assuming all packages have the same maximum
    demand. Thus, we can place `floor(vehicle_capacity / max_demand)` packages
    in a vehicle. Dividing the total number of packages by this we get an
    estimation of how many vehicles we require.

    This heuristic is an overestimation and may be too much in some cases,
    but we found that the solver is more robust in excess (it ignores some
    vehicles if required) than in scarcity (it returns an unfeasible solution).
    """

    num_deliveries = len(instance.deliveries)
    max_demand = max(delivery.size for delivery in instance.deliveries)
    return ceil(num_deliveries / (instance.vehicle_capacity // max_demand))
