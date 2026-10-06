"""Verification suite for rescheduling ("re-plan from now on").

Run:  python3 tests/test_reschedule.py

Covers the properties the feature exists to guarantee:

1. completed history is frozen exactly (zero movement, fixed-start pinned);
2. an undisrupted plan re-scheduled at any freeze point is unchanged;
3. in-progress tasks are continued (remaining duration pinned at `now`);
4. interrupted tasks cut at the breakdown and their tail re-scheduled;
5. breakdown calendars and rush orders are handled and the rush order is
   persisted without polluting the problem definition;
6. perturbation is measurable (abs/max shift, per-task diff);
7. feasibility is never traded away when refining with another solver;
8. round-trip JSON storage preserves the change summary.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import models, seed, storage            # noqa: E402
from backend import reschedule as R                 # noqa: E402
from backend.solvers import base as solver_base     # noqa: E402

# point storage at a throwaway tree
_TMP = tempfile.mkdtemp(prefix="rsched-test-")
storage.INSTANCES_ROOT = os.path.join(_TMP, "instances")
storage.DATA_ROOT = _TMP


def _baseline(pid: str):
    p = storage.load_problem(pid)
    sol = solver_base.get_solver("greedy").solve(p, {})
    storage.save_solution(pid, sol)
    return p, sol


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("PASS" if cond else "FAIL"), "-", name + (f"  [{detail}]" if detail else ""))
    if not cond:
        raise SystemExit(1)


def main() -> None:
    seed.seed_all()
    p, base = _baseline("demo_jobshop")
    ps, bases = _baseline("demo_staffing")

    # 1+2. no disruption -> nothing moves, at several freeze points --------
    for now in (0, 3, 7, 14, 40):
        _, s = R.reschedule(p, base, models.RescheduleRequest(
            baseline_solution_id=base.id, now=now))
        cs = s.change_summary
        check(f"no-disruption now={now}: feasible & zero moves",
              s.status == "feasible" and cs["n_moved"] == 0
              and cs["n_completed"] + cs["n_in_progress"] + cs["n_pending"]
              == len(p.tasks))
        # completed intervals identical to baseline
        for r in cs["rows"]:
            if r["state"] == "completed":
                check(f"  completed {r['task']} frozen", r["change"] == "unchanged")

    # 3. continuing in-progress task pinned at `now` ----------------------
    req = models.RescheduleRequest(
        baseline_solution_id=base.id, now=5,
        progress=[models.TaskProgress(
            task="J2A", state="in_progress",
            actual_start=0, processed=4, remaining=3)])
    _, s = R.reschedule(p, base, req)
    a = next(a for a in s.assignments if a.task == "J2A")
    check("in-progress tail starts at freeze point", a.start == 5 and a.end == 8,
          f"{a.start}->{a.end}")
    check("in-progress tail duration override", a.duration_override == 3)
    row = next(r for r in s.change_summary["rows"] if r["task"] == "J2A")
    check("in-progress state/change reported",
          row["state"] == "in_progress" and row["change"] == "continued")

    # 4. interrupted by breakdown -> tail rescheduled, history kept --------
    req = models.RescheduleRequest(
        baseline_solution_id=base.id, now=2,
        progress=[models.TaskProgress(
            task="J3A", state="interrupted",
            actual_start=0, processed=2, remaining=2)],
        breakdowns=[models.Breakdown(resource="M1", start=2, end=6)])
    _, s = R.reschedule(p, base, req)
    a = next(a for a in s.assignments if a.task == "J3A")
    check("interrupted tail waits for repair", a.start >= 6,
          f"start {a.start}")
    check("interrupted counted in summary",
          s.change_summary["n_interrupted"] == 1)

    # continuing across a breakdown must fail with actionable guidance -----
    req = models.RescheduleRequest(
        baseline_solution_id=base.id, now=2,
        progress=[models.TaskProgress(
            task="J3A", state="in_progress",
            actual_start=0, processed=2, remaining=1)],
        breakdowns=[models.Breakdown(resource="M1", start=2, end=6)])
    try:
        R.reschedule(p, base, req)
        check("continue-on-breakdown rejected", False)
    except R.RescheduleError:
        check("continue-on-breakdown rejected", True)

    # 5. breakdown + rush order: minimal perturbation + clean persistence --
    req = models.RescheduleRequest(
        baseline_solution_id=base.id, now=7,
        breakdowns=[models.Breakdown(resource="M1", start=7, end=11)],
        rush_tasks=[models.RushTask(
            id="J4X", duration=4, resource_requirements={"M2": 1})])
    updated, s = R.reschedule(p, base, req)
    cs = s.change_summary
    check("breakdown+rush feasible", s.status == "feasible")
    check("only forced task J2B moves",
          cs["n_moved"] == 1
          and next(r for r in cs["rows"] if r["task"] == "J2B")["change"] == "moved",
          f"moved={cs['n_moved']} shift={cs['abs_shift']}")
    check("rush task added and scheduled",
          cs["n_new"] == 1
          and next(r for r in cs["rows"] if r["task"] == "J4X")["new_start"] is not None)
    storage.save_problem(updated)
    storage.save_solution("demo_jobshop", s)
    p2 = storage.load_problem("demo_jobshop")
    check("persisted problem adds rush task only",
          any(t.id == "J4X" for t in p2.tasks)
          and not any(c.id.startswith("hc_pin") for c in p2.hard_constraints)
          and not any(c.type == "preferred_start" for c in p2.soft_constraints))
    check("persisted durations untouched",
          next(t for t in p2.tasks if t.id == "J1A").duration == 4)

    # 6. perturbation metrics exist and are consistent --------------------
    moved_rows = [r for r in cs["rows"] if r["change"] == "moved"]
    check("abs_shift equals sum of |delta|",
          cs["abs_shift"] == sum(abs(r["delta"]) for r in moved_rows))
    check("max_shift equals max |delta|",
          cs["max_shift"] == max((abs(r["delta"]) for r in moved_rows), default=0))

    # 7. chain reschedule off the new solution stays stable ---------------
    loaded = storage.load_solution("demo_jobshop", s.id)
    _, s2 = R.reschedule(p2, loaded, models.RescheduleRequest(
        baseline_solution_id=loaded.id, now=11))
    check("chained reschedule feasible & zero extra moves",
          s2.status == "feasible" and s2.change_summary["n_moved"] == 0)

    # 7b. metaheuristic candidate is rejected if infeasible ----------------
    # max_concurrent=2 staffing instance + competing rush task used to let an
    # unconstrained candidate sneak in; the guard must keep the stable plan.
    req = models.RescheduleRequest(
        baseline_solution_id=bases.id, now=14,
        rush_tasks=[models.RushTask(
            id="D1", duration=4, resource_requirements={"P1": 1})],
        solver="genetic", stability_weight=20.0,
        params={"generations": 30, "population_size": 20, "time_limit": 10})
    _, s3 = R.reschedule(ps, bases, req)
    check("refinement never yields infeasible result", s3.status == "feasible")

    # 8. JSON round-trip ----------------------------------------------------
    blob = s2.to_dict()
    again = models.Solution.from_dict(blob)
    check("change_summary survives JSON round-trip",
          again.change_summary is not None
          and again.baseline_solution_id == loaded.id
          and again.rescheduled_at == 11
          and len(again.change_summary["rows"]) == len(blob["change_summary"]["rows"]))

    # 9. invalid inputs -----------------------------------------------------
    bad_cases = [
        models.RescheduleRequest(baseline_solution_id=base.id, now=7,
            progress=[models.TaskProgress(task="GHOST", state="completed")]),
        models.RescheduleRequest(baseline_solution_id=base.id, now=7,
            rush_tasks=[models.RushTask(id="J4Y", duration=4,
                                        resource_requirements={"NOPE": 1})]),
    ]
    for i, req in enumerate(bad_cases):
        try:
            R.reschedule(p, base, req)
            check(f"invalid input #{i} rejected", False)
        except R.RescheduleError:
            check(f"invalid input #{i} rejected", True)

    shutil.rmtree(_TMP, ignore_errors=True)
    print("\nall reschedule checks passed.")


if __name__ == "__main__":
    main()
