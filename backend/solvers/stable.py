"""Stable (minimum-perturbation) scheduler.

A standalone wrapper around the stability-aware serial SGS used by the
rescheduler.  On a fresh problem there is no previous plan to cling to, so it
behaves like an earliest-start greedy with a tie-break toward release time;
its real purpose is to be the default tail solver for rescheduling, where the
``preferred_start`` soft constraints injected by :mod:`backend.reschedule`
give every not-started task an incentive to keep its old slot.
"""

from __future__ import annotations

import time
from typing import Any, Dict

from .. import models, objectives, reschedule
from .base import Solver, register
from . import schedule_builder


@register
class StableSolver(Solver):
    name = "stable"

    def solve(self, problem: models.Problem,
              params: Dict[str, Any]) -> models.Solution:
        t0 = time.time()
        passes = int(params.get("polish_passes", 2))

        # preferred times: explicit preferred_start constraints if present
        # (rescheduled problem), otherwise each task's own release time.
        preferred: Dict[str, int] = {}
        for t in problem.tasks:
            pref = t.release_time
            for c in problem.soft_constraints:
                if c.type == "preferred_start" and c.params.get("task") == t.id:
                    pref = int(c.params.get("start", pref))
            preferred[t.id] = pref

        starts = reschedule.stable_decode(problem, {}, preferred, 0)
        if passes:
            starts = reschedule._polish(problem, starts, preferred, set(),
                                        passes=passes, now=0)
        feasible = len(starts) == len(problem.tasks)
        sol = self.make_solution(
            problem, starts,
            status="feasible" if feasible else "infeasible",
            solve_time=time.time() - t0,
            message="minimum-perturbation stable SGS",
            params=params,
            extra_metrics={"feasible": feasible, "rule": "nearest-preferred"})
        return sol
