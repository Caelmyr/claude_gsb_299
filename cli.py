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

from backend import models, report, reschedule as reschedule_mod, seed, sensitivity, storage
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

    # baseline solution
    baseline = None
    if args.baseline:
        baseline = storage.load_solution(args.id, args.baseline)
        if baseline is None:
            print(f"no such baseline solution: {args.baseline}")
            sys.exit(1)
    else:
        sols = storage.list_solutions(args.id)
        if sols:
            baseline = storage.load_solution(args.id, sols[-1]["id"])

    payload: Dict[str, Any] = {}
    if args.event_file:
        with open(args.event_file, "r", encoding="utf-8") as fh:
            payload = json.load(fh)

    def items(flag: List[str]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for raw in flag:
            k, v = raw.split("=", 1)
            out.append({"task": k, **json.loads(v)})
        return out

    progress = [models.TaskProgress.from_dict(x)
                for x in (payload.get("progress", []) + items(args.progress))]
    downtimes = [models.ResourceDowntime.from_dict(x)
                 for x in (payload.get("downtimes", [])
                           + [{"resource": r, **json.loads(v)}
                              for r, v in (x.split("=", 1) for x in args.downtime)])]
    new_tasks = payload.get("new_tasks", [])
    params = dict(_parse_params(args.params))

    result = reschedule_mod.reschedule(
        p, args.now, baseline=baseline, progress=progress,
        downtimes=downtimes, new_tasks=new_tasks,
        solver=args.solver, params=params,
        baseline_solution_id=args.baseline)
    storage.save_reschedule(args.id, result)

    s = result.summary
    print(f"reschedule {result.id} @now={result.now} solver={result.solver} "
          f"status={result.status} makespan={s['makespan']} "
          f"(基线 {s['baseline_makespan']}, Δ {s['makespan_delta']})")
    print(f"  冻结: 已完成 {s['n_completed']} / 在制 {s['n_in_progress']}；"
          f"未动 {s['n_unchanged']}，改动 {s['n_changed']}，急单 {s['n_added']}，"
          f"排不进 {s['n_unscheduled']}")
    print(f"  改动量: 总位移 {s['total_abs_shift']}，最大单任务位移 "
          f"{s['max_abs_shift']}，总延后 {s['total_delay']}")
    for c in result.changes:
        tag = {"completed": "✓完成", "in_progress": "▶在制", "unchanged": " 未动",
               "delayed": "⌚延后", "advanced": "⏫提前", "added": "★急单",
               "unscheduled": "✗排不进"}.get(c.change_type, c.change_type)
        print(f"  [{tag}] {c.task:8s} {str(c.old_start):>4s} -> "
              f"{str(c.new_start):>4s} Δ={c.delta:<4d} {c.note}")
    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))


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

    p_res = sub.add_parser("reschedule",
                           help="从指定时点起对未开始任务做滚动重排")
    p_res.add_argument("id")
    p_res.add_argument("--now", type=int, required=True)
    p_res.add_argument("--baseline", help="基线方案 id（默认取最新 solution）")
    p_res.add_argument("--solver", default="stability",
                       choices=list(models.RESCHEDULE_SOLVERS))
    p_res.add_argument("--progress", nargs="*", default=[],
                       help='task={"status":"completed","actual_end":5}')
    p_res.add_argument("--downtime", nargs="*", default=[],
                       help='M1={"start":8,"end":12,"reason":"故障"}')
    p_res.add_argument("--event-file", help="含 progress/downtimes/new_tasks 的 JSON")
    p_res.add_argument("--params", nargs="*", default=[])
    p_res.add_argument("--json", action="store_true")
    p_res.set_defaults(func=cmd_reschedule)

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
