"""Solvers package.  Importing this package registers all solvers."""

from . import (  # noqa: F401  (import for side-effect: registration)
    base,
    greedy,
    stable,
    genetic,
    simulated_annealing,
    lp,
    ip,
    simplex,
    schedule_builder,
    time_indexed,
)

__all__ = ["base", "greedy", "stable", "genetic", "simulated_annealing",
           "lp", "ip", "simplex", "schedule_builder", "time_indexed"]
