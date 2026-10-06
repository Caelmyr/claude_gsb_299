"""Headless CLI for the scheduling system.

Examples::

    python cli.py seed --force
    python cli.py list
    python cli.py solve demo_jobshop --solver genetic --params generations=300
    python cli.py sensitivity demo_jobshop --kind resource_capacity --resource M1
    python cli.py report demo_jobshop
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict, List

from backend import models, report, reschedule as resched_mod, seed, sensitivity, storage
from backend.solvers import base as solver_base


def _parse_params(items: List[str]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for item in items:
        if "=" not in item:
            continue
        k, v = item.split("=", 1)
        # coerce obvious scalars
        for cast in (int, float):
            try:
                v = cast(v)  # type: ignore[assignment]
                break
            except ValueError:
                continue
        if v == "true":
            v = True
        elif v == "false":
            v = False
        out[k] = v
    return out


def cmd_seed(args) -> None:
    created = seed.seed_all(force=args.force)
    print(f"seeded {len(created)} instance(s): {', '.join(created) or '(none new)'}")


def cmd_list(args) -> None:
    for p in storage.list_problems():
        print(f"{p['id']:16s} v{p['version']:<3d} tasks={p['n_tasks']:<3d} "
              f"res={p['n_resources']:<3d} obj={p['objective']:20s} {p['name']}")


def cmd_show(args) -> None:
    p = storage.load_problem(args.id, args.version)
    if p is None:
        print(f"no such problem: {args.id}")
        sys.exit(1)
    print(json.dumps(p.to_dict(), ensure_ascii=False, indent=2))


def cmd_solve(args) -> None:
    p = storage.load_problem(args.id)
    if p is None:
        print(f"no such problem: {args.id}")
        sys.exit(1)
    params = dict(solver_base.default_params(args.solver))
    params.update(_parse_params(args.params))
    solver = solver_base.get_solver(args.solver)
    sol = solver.solve(p, params)
    storage.save_solution(args.id, sol)
    print(f"solution {sol.id}: solver={sol.solver} status={sol.status} "
          f"objective={sol.objective_value} makespan={sol.makespan} "
          f"time={sol.solve_time}s")
    if args.json:
        print(json.dumps(sol.to_dict(), ensure_ascii=False, indent=2))


def cmd_solutions(args) -> None:
    for s in storage.list_solutions(args.id):
        print(f"{s['id']}  {s['solver']:20s} {s['status']:10s} "
              f"obj={s['objective_value']} mk={s['makespan']} t={s['solve_time']}s")


def cmd_reschedule(args) -> None:
    p = storage.load_problem(args.id)
    if p is None:
        print(f"no such problem: {args.id}")
        sys.exit(1)
    baseline = storage.load_solution(args.id, args.baseline)
    if baseline is None:
        print(f"no such baseline solution: {args.baseline}")
        sys.exit(1)

    progress = []
    for item in args.progress:
        # STATE:TASK[:processed[:remaining]] e.g.
        #   completed:J1A   interrupted:J2B:3   in_progress:J3A:2:5
        parts = item.split(":")
        if len(parts) < 2:
            print(f"bad --progress value: {item}")
            sys.exit(2)
        state, tid = parts[0], parts[1]
        processed = int(parts[2]) if len(parts) > 2 and parts[2] != "" else 0
        remaining = int(parts[3]) if len(parts) > 3 and parts[3] != "" else None
        progress.append(models.TaskProgress(
            task=tid, state=state, processed=processed, remaining=remaining))

    breakdowns = [
        models.Breakdown(resource=r, start=s, end=e)
        for (r, s, e) in args.breakdown
    ]

    rush_tasks = []
    for item in args.rush:
        # ID:DURATION:RES[,RES...]   e.g. J9X:4:M1
        parts = item.split(":")
        if len(parts) < 2:
            print(f"bad --rush value: {item}")
            sys.exit(2)
        rid, dur = parts[0], int(parts[1])
        res = {x: 1 for x in (parts[2].split(",") if len(parts) > 2 and parts[2] else [])}
        deps = parts[3].split(",") if len(parts) > 3 and parts[3] else []
        rush_tasks.append(models.RushTask(
            id=rid, duration=dur, resource_requirements=res, dependencies=deps))

    req = models.RescheduleRequest(
        baseline_solution_id=baseline.id, now=args.now, progress=progress,
        breakdowns=breakdowns, rush_tasks=rush_tasks,
        solver=args.solver, stability_weight=args.stability_weight,
        params=_parse_params(args.params))
    try:
        updated, sol = resched_mod.reschedule(p, baseline, req)
    except ValueError as exc:
        print(f"reschedule failed: {exc}")
        sys.exit(1)

    if rush_tasks:
        storage.save_problem(updated)
        print(f"problem updated to v{updated.version} (+{len(rush_tasks)} rush task(s))")
    storage.save_solution(args.id, sol)
    cs = sol.change_summary or {}
    print(f"solution {sol.id}: solver={sol.solver} status={sol.status} "
          f"objective={sol.objective_value} makespan={sol.makespan}")
    print(f"  frozen @t={args.now}: completed={cs.get('n_completed')} "
          f"in_progress={cs.get('n_in_progress')} "
          f"interrupted={cs.get('n_interrupted')} pending={cs.get('n_pending')}")
    print(f"  changes: moved={cs.get('n_moved')} added={cs.get('n_new')} "
          f"abs_shift={cs.get('abs_shift')} max_shift={cs.get('max_shift')} "
          f"objective_delta={cs.get('objective_delta')}")
    for row in cs.get("rows", []):
        if row["change"] in ("unchanged",):
            continue
        print(f"  {row['task']:8s} {row['change']:12s} "
              f"base={row['base_start']}->{row['base_end']} "
              f"new={row['new_start']}->{row['new_end']} Δ={row['delta']}")
    if args.json:
        print(json.dumps(sol.to_dict(), ensure_ascii=False, indent=2))


def cmd_sensitivity(args) -> None:
    p = storage.load_problem(args.id)
    if p is None:
        print(f"no such problem: {args.id}")
        sys.exit(1)
    spec: Dict[str, Any] = {"kind": args.kind}
    if args.resource:
        spec["resource"] = args.resource
    if args.task:
        spec["task"] = args.task
    result = sensitivity.run_sensitivity(p, args.solver, spec, persist=True)
    print(f"sensitivity {result.id} (base obj {result.base_objective}):")
    for v in result.variations:
        print(f"  {v['label']:24s} obj={v['objective_value']} "
              f"delta={v['delta']} status={v['status']}")


def cmd_report(args) -> None:
    p = storage.load_problem(args.id)
    if p is None:
        print(f"no such problem: {args.id}")
        sys.exit(1)
    sols = [storage.load_solution(args.id, sid) for sid in args.solutions]
    sols = [s for s in sols if s is not None]
    rep = report.generate_report(p, sols)
    print(rep.content)


def main() -> None:
    parser = argparse.ArgumentParser(description="OR scheduling CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_seed = sub.add_parser("seed")
    p_seed.add_argument("--force", action="store_true")
    p_seed.set_defaults(func=cmd_seed)

    p_list = sub.add_parser("list")
    p_list.set_defaults(func=cmd_list)

    p_show = sub.add_parser("show")
    p_show.add_argument("id")
    p_show.add_argument("--version", type=int)
    p_show.set_defaults(func=cmd_show)

    p_solve = sub.add_parser("solve")
    p_solve.add_argument("id")
    p_solve.add_argument("--solver", default="greedy")
    p_solve.add_argument("--params", nargs="*", default=[])
    p_solve.add_argument("--json", action="store_true")
    p_solve.set_defaults(func=cmd_solve)

    p_sol = sub.add_parser("solutions")
    p_sol.add_argument("id")
    p_sol.set_defaults(func=cmd_solutions)

    p_rs = sub.add_parser("reschedule",
                          help="re-plan from a freeze point, keeping history")
    p_rs.add_argument("id")
    p_rs.add_argument("baseline", help="baseline solution id")
    p_rs.add_argument("--now", type=int, required=True, help="freeze point")
    p_rs.add_argument("--progress", nargs="*", default=[],
                      help="STATE:TASK[:processed[:remaining]] per override")
    p_rs.add_argument("--breakdown", nargs="*", default=[],
                      type=lambda s: tuple(int(x) if i else x
                                           for i, x in enumerate(s.split(":"))),
                      help="RESOURCE:START:END (repeatable)")
    p_rs.add_argument("--rush", nargs="*", default=[],
                      help="ID:DURATION[:RES1,RES2[:DEP1,DEP2]]")
    p_rs.add_argument("--solver", default="stable",
                      choices=["stable", "greedy", "genetic",
                               "simulated_annealing", "lp", "ip"])
    p_rs.add_argument("--stability-weight", type=float, default=10.0,
                      dest="stability_weight")
    p_rs.add_argument("--params", nargs="*", default=[])
    p_rs.add_argument("--json", action="store_true")
    p_rs.set_defaults(func=cmd_reschedule)

    p_sens = sub.add_parser("sensitivity")
    p_sens.add_argument("id")
    p_sens.add_argument("--kind", default="resource_capacity")
    p_sens.add_argument("--resource")
    p_sens.add_argument("--task")
    p_sens.add_argument("--solver", default="greedy")
    p_sens.set_defaults(func=cmd_sensitivity)

    p_rep = sub.add_parser("report")
    p_rep.add_argument("id")
    p_rep.add_argument("--solutions", nargs="*", default=[])
    p_rep.set_defaults(func=cmd_report)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
