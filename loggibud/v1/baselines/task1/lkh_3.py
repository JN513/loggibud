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
from typing import Dict, Optional

from loggibud.v1.types import (
    CVRPInstance,
    CVRPSolution,
    CVRPSolutionVehicle,
    JSONDataclassMixin,
)
from loggibud.v1.data_conversion import to_tsplib


logger = logging.getLogger(__name__)


DEPOT_NODE = 1


@dataclass
class LKHParams(JSONDataclassMixin):
    # Time limit in seconds to step the solver
    time_limit_s: int = 60

    # Number of runs (as in a multistart heuristic)
    num_runs: int = 1


def solve(
    instance: CVRPInstance, params: Optional[LKHParams] = None
) -> CVRPSolution:
    """Solve a CVRP instance using LKH-3"""

    params = params or LKHParams()

    logger.info("Converting instance into a TSPLIB file")
    convert_instance_file(instance, params)

    logger.info("Calling LKH external solver")
    solve_lkh(instance, params)

    logger.info("Reading result from output files")
    solution = read_solution(instance, params)

    logger.info("Cleaning up temporary files")
    remove_auxiliary_files(instance)

    return solution


def convert_instance_file(instance: CVRPInstance, params: LKHParams) -> None:
    """
    The LKH-3 solver requires two files:
        - A .par file with parameters for the solver;
        - A .vrp file with information about the instance.
    The vrp file is created with the original TSPLIB format, and the .par one,
    which is specific for the LKH-3 solver, is created here.
    These files will have the instance name to prevent conflicts in case the
    solver runs in parallel solving multiple instances.
    """

    auxiliary_file_names = _get_auxiliary_file_names(instance)
    to_tsplib(instance, file_name=auxiliary_file_names["input_vrp_file"])

    # For the Asymetric CVRP, it only respects the capacity if the number of
    # vehicles is explicitly provided
    num_vehicles = _get_num_vehicles(instance)
    with open(auxiliary_file_names["input_par_file"], "w") as f:
        f.write(
            "SPECIAL\n"
            f"PROBLEM_FILE = {auxiliary_file_names['input_vrp_file']}\n"
            "MTSP_OBJECTIVE = MINSUM\n"
            f"RUNS = {params.num_runs}\n"
            f"TOUR_FILE = {auxiliary_file_names['output_tour_file']}\n"
            f"TIME_LIMIT = {params.time_limit_s}\n"
            f"VEHICLES = {num_vehicles}"
        )


def solve_lkh(instance: CVRPInstance, params: LKHParams) -> None:
    """Call the C solver and generate output files"""

    auxiliary_file_names = _get_auxiliary_file_names(instance)
    arguments = (
        "./loggibud/v1/baselines/task1/LKH",
        auxiliary_file_names["input_par_file"],
    )
    popen = subprocess.Popen(arguments, stdout=subprocess.PIPE)
    popen.wait()  # run the solver in the background


def read_solution(instance: CVRPInstance, params: LKHParams) -> CVRPSolution:
    """Read the files generated by the solver

    Notes
    -----
    The output is stored in a TSPLIB-like format. Here is a typical example.

    Suppose a problem with depot at node 1 and deliveries at 2, 3, 4, 5 and 6.
    Now, suppose the solution has two routes such as:
        - Route 1: [1, 2, 3]
        - Route 2: [1, 4, 5, 6]

    The output would be written in a file with the following:

        TOUR_SECTION
        1
        2
        3
        7 <---
        4
        5
        6
        -1

    The first node is 1, the depot, and the following are deliveries in the
    first route. Then, we reach a node 7, which is greater than all nodes in
    the problem. This actually marks the start of another route, and if we had
    more routes, it would be split with an 8, and so on.

    The reading goes on until a -1 is obtained, thus marking the end of all
    routes.

    Our reading process goes like:
        1. Read all output lines until the `TOUR_SECTION` begins;
        2. Read the nodes, converting each node larger than the number of
        locations into 1 until we reach a -1.
        3. The final list would be [1, 2, 3, 1, 4, 5, 6]. Then, split this
        list into subgroups with respect to the DEPOT_NODE 1, such as
        [2, 3], [4, 5, 6]. These will be the nodes of final routes.
    """

    auxiliary_file_names = _get_auxiliary_file_names(instance)
    num_locations = len(instance.deliveries) + 1
    with open(auxiliary_file_names["output_tour_file"], "r") as f:
        # Ignore the header until we reach `TOUR_SECTION`
        for line in f:
            if line.startswith("TOUR_SECTION"):
                break

        # Read nodes in order replacing large numbers with DEPOT_NODE
        def read_delivery_nodes_gen():
            for line in f:
                node = int(line.rstrip("\n"))
                if node == -1:
                    break

                yield node if node <= num_locations else DEPOT_NODE

        all_route_nodes = list(read_delivery_nodes_gen())

        # Split the previous list with respect to `DEPOT_NODE`
        def write_route(nodes_group):
            # Notice the deliveries start at 2 in the output file but at 0 in
            # the instance
            deliveries = [
                instance.deliveries[node - 2] for node in nodes_group
            ]
            return CVRPSolutionVehicle(
                origin=instance.origin, deliveries=deliveries
            )

        nodes_groups = groupby(all_route_nodes, key=lambda x: x == DEPOT_NODE)
        routes = [
            write_route(nodes_group)
            for key, nodes_group in nodes_groups
            if not key
        ]

    return CVRPSolution(name=instance.name, vehicles=routes)


def remove_auxiliary_files(instance: CVRPInstance) -> None:
    auxiliary_file_names = _get_auxiliary_file_names(instance)
    for file_name in auxiliary_file_names.values():
        os.remove(file_name)


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


def _get_auxiliary_file_names(instance: CVRPInstance) -> Dict[str, str]:
    return {
        "input_vrp_file": f"vrp_input_temp_{instance.name}.vrp",
        "input_par_file": f"vrp_input_temp_{instance.name}.par",
        "output_tour_file": f"vrp_output_temp_{instance.name}.vrp",
    }
